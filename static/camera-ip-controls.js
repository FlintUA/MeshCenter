// ============================================================
// Network camera controls in the Camera card (CAM2 Phase 3b).
//
// A network camera has no CSI/USB mode or image controls (they stay hidden, see camera-ip.js), so this module
// renders what such a camera really offers, below the picture. The tab row is the card's one shared row (Mode /
// Image / Lens, template + chat-camera.js); this module only fills the panels:
//
//   Mode  - what the stream is (stream picker with Reconnect, resolution, frame rate, codec, bitrate, state), the
//           Snapshot button with its quality setting (full / small) and a hint that follows the real situation,
//           and the note that image settings are changed in the camera app.
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
    // {position, settled, superseded}. Both may throw an object with `error_code` / `right`.
    //
    // LENS1: a request made while the lens is still moving does not wait for the old move to be read back. The
    // latest target wins: it is sent at most `minGapMs` (the camera's own minimum interval) after the previous
    // command, the server then ends the older wait at once (that answer is `superseded` and ignored here) and
    // reads the NEW target back. At most two moves are in flight. A permission error ends the control.
    function createLensController(options) {
        const minGapMs = options.minGapMs != null ? options.minGapMs : 500;
        const now = options.now || Date.now;
        const state = { phase: 'idle', position: null, range: null, focus: null, target: null, settled: true, error: null, denied: false };
        let pending = null;
        let inflight = 0;
        let moveSeq = 0;
        let reading = false;
        let lastSendAt = -Infinity;
        let timer = null;

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

        async function send(target) {
            const seq = ++moveSeq;
            inflight += 1;
            lastSendAt = now();
            state.phase = 'moving';
            state.target = target;
            state.error = null;
            emit();
            try {
                const result = await options.move(target);
                if (result.range) state.range = result.range; // the range is true whichever answer carries it
                if (seq === moveSeq && !result.superseded) {
                    apply(result);
                    state.settled = result.settled !== false;
                    state.phase = 'ready';
                }
            } catch (error) {
                if (seq === moveSeq || (error && error.error_code === 'permission_denied')) fail(error);
            }
            inflight -= 1;
            if (seq === moveSeq) state.target = null;
            emit();
            drain();
        }

        function drain() {
            if (pending === null || state.denied || reading || inflight >= 2) return;
            const wait = lastSendAt + minGapMs - now();
            if (wait > 0) {
                if (!timer) timer = setTimeout(() => { timer = null; drain(); }, wait);
                return;
            }
            const target = pending;
            pending = null;
            if (state.range && (target < state.range[0] || target > state.range[1])) return drain();
            if (inflight === 0 && target === state.position && state.settled) return;
            send(target);
        }

        return {
            state,
            // read the position (tab opened, or once more after a lens that had not arrived)
            async refresh() {
                if (inflight > 0 || reading || state.denied) return;
                reading = true;
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
                reading = false;
                emit();
                drain();
            },
            // the user chose a position (slider released): the latest one wins
            request(target) {
                if (!Number.isInteger(target) || state.denied) return Promise.resolve();
                pending = target;
                drain();
                return Promise.resolve();
            },
            busy() { return inflight > 0 || reading; },
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

    // The stream picker (sub / main) and Reconnect live in the Mode tab. camera-ip.js owns their behaviour (it
    // listens on the document, so a rebuilt panel needs no re-wiring) and fills the option texts.
    function streamField() {
        const select = el('select', { id: 'ipCameraProfile', class: 'ip-select', 'aria-label': t('ipctl.info_stream') }, [
            el('option', { value: 'sub', text: t('ipcamera.profile_sub') }),
            el('option', { value: 'main', text: t('ipcamera.profile_main') }),
        ]);
        const reconnect = el('button', { type: 'button', class: 'device-action-btn device-action-secondary ip-reconnect-btn', id: 'ipCameraReconnectBtn', text: t('ipctl.info_reconnect') });
        reconnect.style.display = 'none';
        return el('div', { class: 'ip-field ip-field-stream' }, [
            el('label', { class: 'ip-field-label', for: 'ipCameraProfile', text: t('ipctl.info_stream') }), select, reconnect,
        ]);
    }

    function build(status, container) {
        container.textContent = '';
        const hasLens = Boolean(status.zoom_control);
        const hasApi = status.control_type === 'reolink';
        const quality = hasApi ? el('select', { id: 'ipSnapshotQuality', class: 'ip-select', 'aria-label': t('ipctl.quality_label') }, [
            el('option', { value: 'full', text: t('ipctl.quality_full') }),
            el('option', { value: 'small', text: t('ipctl.quality_small') }),
        ]) : null;
        // One compact row: Stream [select] - Take picture - Picture quality [select]. The facts (resolution, fps, codec,
        // bitrate, state) are in the status line of the tab bar; what Take picture does is one small muted line.
        const modePanel = el('div', { class: 'ip-panel', id: 'ipPanelMode', role: 'tabpanel', 'aria-labelledby': 'cameraControlTabCamera' }, [
            el('div', { class: 'ip-mode-row' }, [
                streamField(),
                el('button', { type: 'button', class: 'device-action-btn device-action-primary is-primary ip-snapshot-btn', id: 'ipSnapshotBtn', text: t('ipctl.snapshot_button') }),
                hasApi ? el('label', { class: 'ip-field ip-quality', for: 'ipSnapshotQuality' }, [el('span', { class: 'ip-field-label', text: t('ipctl.quality_label') }), quality]) : null,
            ]),
            el('div', { class: 'ip-hint ip-stream-hint', id: 'ipStreamHint', text: '' }),
            el('div', { class: 'ip-hint ip-mode-hint', id: 'ipSnapshotHint', text: '' }),
        ]);

        const slider = el('input', { type: 'range', id: 'ipLensSlider', class: 'ip-lens-slider', min: '0', max: '0', step: '1', value: '0', 'aria-label': t('ipctl.lens_slider_label') });
        slider.disabled = true;
        const lensPanel = hasLens ? el('div', { class: 'ip-panel', id: 'ipPanelLens', role: 'tabpanel', 'aria-labelledby': 'cameraControlTabLens' }, [
            el('div', { class: 'ip-lens-value', id: 'ipLensValue', text: '-' }),
            slider,
            el('div', { class: 'ip-lens-focus', id: 'ipLensFocus', text: '' }),
            el('div', { class: 'ip-hint', text: t('ipctl.lens_hint') }),
            el('div', { class: 'ip-lens-status', id: 'ipLensStatus', role: 'status', 'aria-live': 'polite', text: '' }),
        ]) : null;

        container.appendChild(modePanel);
        if (lensPanel) container.appendChild(lensPanel);

        const lensTab = byId('cameraControlTabLens');
        if (lensTab) {
            lensTab.hidden = !hasLens;
            if (!lensTab.dataset.wired) { // the tab belongs to the template and outlives a rebuilt panel
                lensTab.dataset.wired = '1';
                lensTab.addEventListener('click', () => {
                    if (typeof window.switchCameraControlTab === 'function') window.switchCameraControlTab('lens');
                    else selectTab('lens');
                });
            }
        }
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
        const modeTab = byId('cameraControlTabCamera');
        const lensTab = byId('cameraControlTabLens');
        const imageTab = byId('cameraControlTabImage');
        const modePanel = byId('ipPanelMode');
        const lensPanel = byId('ipPanelLens');
        const showLens = activeTab === 'lens';
        if (modeTab) { modeTab.classList.toggle('active', !showLens); modeTab.setAttribute('aria-selected', String(!showLens)); }
        if (lensTab) { lensTab.classList.toggle('active', showLens); lensTab.setAttribute('aria-selected', String(showLens)); }
        if (imageTab) { imageTab.classList.toggle('active', false); imageTab.setAttribute('aria-selected', 'false'); }
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
        const snap = window.MCIpCamera && typeof window.MCIpCamera.snapshotInfo === 'function' ? window.MCIpCamera.snapshotInfo() : null;
        // the sub / main choice means nothing for pictures (the camera makes them): locked, with one line why
        const select = byId('ipCameraProfile');
        if (select && select.disabled !== Boolean(snap)) select.disabled = Boolean(snap);
        const hint = byId('ipStreamHint');
        if (hint) {
            hint.hidden = !snap;
            setText('ipStreamHint', snap ? t(snap.reason === 'ffmpeg' ? 'ipctl.stream_hint_snapshot_ffmpeg' : 'ipctl.stream_hint_snapshot_browser') : '');
        }
        const helpers = window.MCIpCamera && window.MCIpCamera._helpers;
        const quality = byId('ipSnapshotQuality');
        const wanted = localQuality || status.snapshot_quality || 'full';
        if (quality) {
            if (localQuality && localQuality === status.snapshot_quality) localQuality = null;
            if (quality.value !== wanted) quality.value = wanted;
        }
        // what Take picture does, and where the image settings live, in one small muted line
        const hintKey = snapshotHintKey(status, wanted);
        setText('ipSnapshotHint', t(hintKey, hintKey.indexOf('stream_running') !== -1 ? { resolution: status.resolution || '?' } : undefined) + ' ' + t('ipctl.image_note'));
        const reconnect = byId('ipCameraReconnectBtn');
        if (reconnect && helpers) reconnect.style.display = helpers.isTerminal(status) || status.state === 'reconnecting' ? '' : 'none';
    }

    // What Take picture will really do right now: the camera itself makes the picture (Reolink control), or it is
    // cut from the running preview (so the preview has to run first).
    function snapshotHintKey(status, quality) {
        if (status.control_type === 'reolink') return quality === 'small' ? 'ipctl.snapshot_hint_api_small' : 'ipctl.snapshot_hint_api_full';
        if (status.state !== 'live') return 'ipctl.snapshot_hint_stream_stopped';
        return status.profile === 'main' ? 'ipctl.snapshot_hint_stream_running_main' : 'ipctl.snapshot_hint_stream_running';
    }

    // ---------- Mode: the picture ----------

    // The toast always names the real size and where the picture came from: the camera's own full / small picture,
    // the running stream (no camera function), or the running stream because the camera function failed (fallback).
    function savedToastKey(body) {
        if (body.method === 'camera_api_small') return 'ipctl.snapshot_saved_api_small';
        if (body.method === 'camera_api_full') return 'ipctl.snapshot_saved_api_full';
        return body.fallback ? 'ipctl.snapshot_saved_stream' : 'ipctl.snapshot_saved_stream_only';
    }

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
                showToast(t(savedToastKey(body), { resolution: body.resolution || '?' }), body.fallback ? 'warning' : 'success', body.fallback ? { duration: 10000 } : undefined);
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
        return { position: body.position, settled: body.settled, range: body.zoom_range, superseded: Boolean(body.superseded) };
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
        const lensTab = byId('cameraControlTabLens');
        if (lensTab) lensTab.hidden = true;
        if (rereadTimer) { clearTimeout(rereadTimer); rereadTimer = null; }
    }

    function update(status) {
        const container = byId('ipCameraControls');
        if (!container) return;
        if (!status || status.source_type !== 'rtsp' || !status.source_id) {
            const wasShown = Boolean(built);
            hide();
            built = null;
            lens = null;
            // back to a local camera: the shared tab row returns to Mode
            if (wasShown && typeof window.switchCameraControlTab === 'function') window.switchCameraControlTab('camera');
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
