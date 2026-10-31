// tests/frontend/test_camera_ip_controls.mjs
//
// CAM2 Phase 3b: static/camera-ip-controls.js - the network camera's Mode / Lens panel in the Camera card.
// Runs the REAL file in a vm sandbox with a tiny fake DOM. `node tests/frontend/test_camera_ip_controls.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');
const source = read('camera-ip-controls.js');
const catalog = JSON.parse(readFileSync(path.join(__dirname, '..', '..', 'static', 'i18n', 'en.json'), 'utf8'));

// ---------------------------------------------------------------- fake DOM
function makeEnv({ fetchImpl } = {}) {
    const registry = {};
    class FakeEl {
        constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.hidden = false; this.disabled = false; this.value = ''; this.className = ''; this._text = ''; this.min = ''; this.max = ''; this.dataset = {}; this.style = {}; }
        setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') registry[v] = this; if (k === 'value') this.value = String(v); if (k === 'class') this.className = String(v); }
        getAttribute(k) { return this.attrs[k]; }
        appendChild(child) { this.children.push(child); return child; }
        set textContent(v) { this._text = String(v); if (v === '') this.children = []; }
        get textContent() { return this._text; }
        addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
        fire(name, event = {}) { (this.listeners[name] || []).forEach((fn) => fn(Object.assign({ target: this }, event))); }
        get classList() { const self = this; return { toggle(n, on) { const set = new Set(self.className.split(' ').filter(Boolean)); if (on) set.add(n); else set.delete(n); self.className = [...set].join(' '); }, contains(n) { return self.className.split(' ').includes(n); } }; }
    }
    const toasts = [];
    const calls = [];
    const document = { createElement: (tag) => new FakeEl(tag), getElementById: (id) => registry[id] || null, activeElement: null };
    const container = new FakeEl('div');
    container.hidden = true;
    registry.ipCameraControls = container;
    // the shared tab row belongs to the template (UX3): Mode / Image / Lens, the Lens tab hidden until shown
    for (const id of ['cameraControlTabCamera', 'cameraControlTabImage', 'cameraControlTabLens']) {
        const tab = new FakeEl('button');
        tab.attrs.id = id;
        registry[id] = tab;
    }
    registry.cameraControlTabLens.hidden = true;
    const win = {
        document,
        I18N: { t: (key, params) => (params ? key + ':' + JSON.stringify(params) : key) },
        MCIpCamera: { _helpers: { isTerminal: () => false, stateMessageKey: (st) => 'ipcamera.state_' + st.state } },
        showToast: (message, type) => toasts.push([message, type]),
        setTimeout, clearTimeout, console, Promise,
        fetch: async (url, options) => { calls.push({ url, options: options || {} }); return fetchImpl(url, options || {}); },
        mediaGalleryLoaded: true,
    };
    win.window = win;
    vm.createContext(win);
    vm.runInContext(source, win);
    return { win, registry, toasts, calls, container, api: win.MCIpControls };
}

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
const STATUS = { source_type: 'rtsp', source_id: 'ip:1', state: 'live', profile: 'sub', resolution: '640x360', fps: 15.1, codec: 'h264', bitrate_kbps: 139, control_type: 'reolink', snapshot_quality: 'full', zoom_control: true };
const tick = (ms = 5) => new Promise((resolve) => setTimeout(resolve, ms));

// ---------------------------------------------------------------- the lens controller (LENS1: latest wins, no waiting for the old move)
{
    const env = makeEnv();
    const log = [];
    let running = 0;
    let maxParallel = 0;
    const gates = [];
    const lens = env.api.createLensController({
        minGapMs: 20,
        read: async () => ({ position: 22, range: [0, 28], focus: 222 }),
        move: (target) => new Promise((resolve) => {
            running += 1;
            maxParallel = Math.max(maxParallel, running);
            log.push(target);
            // the server ends the older wait when a newer target arrives: that answer is `superseded`
            gates.push((superseded) => { running -= 1; resolve(superseded ? { position: null, settled: false, superseded: true } : { position: target, settled: true }); });
        }),
        onChange: () => {},
    });
    await lens.refresh();
    assert.equal(lens.state.position, 22);
    assert.equal(JSON.stringify(lens.state.range), '[0,28]');
    assert.equal(lens.state.focus, 222);
    lens.request(10); // sent at once
    await tick();
    assert.equal(lens.state.phase, 'moving');
    assert.equal(JSON.stringify(log), '[10]');
    lens.request(11);
    lens.request(12);
    lens.request(15); // dragged again while the lens moves: only the latest survives, and it does NOT wait for the move to 10
    await tick(60);
    assert.equal(JSON.stringify(log), '[10,15]', 'the newer target is sent while the older one is still in flight');
    assert.equal(maxParallel, 2, 'at most two moves in flight');
    gates.shift()(true); // the server answers the older one as superseded
    await tick();
    assert.equal(lens.state.phase, 'moving', 'the superseded answer ends nothing: the latest target is still being read back');
    assert.equal(lens.state.position, 22, 'and it is not shown as a position');
    gates.shift()(false); // the read-back of the last target
    await tick();
    assert.equal(lens.state.position, 15);
    assert.equal(lens.state.phase, 'ready');
    lens.request(15); // already there: nothing is sent
    lens.request(99); // outside the range: nothing is sent
    await tick(60);
    assert.equal(JSON.stringify(log), '[10,15]');
}

// a permission error ends the control; other errors are shown and the next request still works
{
    const env = makeEnv();
    let calls = 0;
    const lens = env.api.createLensController({
        minGapMs: 0,
        read: async () => ({ position: 5, range: [0, 28], focus: 1 }),
        move: async () => { calls += 1; throw { error_code: 'permission_denied', right: 'lens_control', status: 403 }; },
    });
    await lens.refresh();
    lens.request(8);
    await tick();
    assert.equal(lens.state.denied, true);
    assert.equal(lens.state.error.right, 'lens_control');
    lens.request(9);
    await tick();
    await lens.refresh();
    assert.equal(calls, 1, 'no more commands after a permission error');

    const other = env.api.createLensController({
        minGapMs: 0,
        read: async () => ({ position: 5, range: [0, 28] }),
        move: async (target) => { if (target === 8) throw { error_code: 'unreachable' }; return { position: target, settled: true }; },
    });
    await other.refresh();
    other.request(8);
    await tick();
    assert.equal(other.state.phase, 'error');
    assert.equal(other.state.denied, false);
    other.request(9);
    await tick();
    assert.equal(other.state.phase, 'ready');
    assert.equal(other.state.position, 9);
}

// ---------------------------------------------------------------- snapshot error messages
{
    const { api } = makeEnv();
    assert.equal(JSON.stringify(api.snapshotError({ error_code: 'rate_limited', retry_after_s: 1.2 })), JSON.stringify({ key: 'ipctl.snap_err_rate_limited', params: { seconds: 2 } }));
    for (const [code, key] of [['storage_full', 'ipctl.snap_err_storage_full'], ['preview_not_running', 'ipctl.snap_err_preview_not_running'],
        ['camera_off', 'ipctl.snap_err_camera_off'], ['ffmpeg_missing', 'ipctl.snap_err_ffmpeg_missing'], ['frame_grab_failed', 'ipctl.snap_err_frame_grab_failed']]) {
        assert.equal(api.snapshotError({ error_code: code }).key, key);
    }
    assert.equal(api.snapshotError({ error_code: 'weird' }).params.code, 'weird');
    assert.equal(api.snapshotError({ error_code: 'permission_denied', right: 'snapshot' }).control, true);
    assert.equal(api.errorMessage({ error_code: 'storage_full' }), 'ipctl.snap_err_storage_full');
}

// ---------------------------------------------------------------- the panel
{
    const env = makeEnv({ fetchImpl: async () => json(200, { ok: true }) });
    assert.equal(env.container.hidden, true);
    env.api.update(STATUS);
    assert.equal(env.container.hidden, false);
    assert.equal(env.registry.cameraControlTabLens.hidden, false, 'the shared Lens tab is shown for a camera with zoom');
    assert.equal(env.registry.ipTabMode, undefined, 'no tab row of its own any more');
    assert.ok(env.registry.ipCameraProfile && env.registry.ipCameraReconnectBtn, 'stream picker and Reconnect are in the Mode panel');
    assert.equal(env.registry.ipCameraProfile.children.length, 2);
    assert.equal(env.registry.ipCameraReconnectBtn.style.display, 'none', 'Reconnect only while the stream is down');
    for (const id of ['ipInfoResolution', 'ipInfoFps', 'ipInfoCodec', 'ipInfoBitrate', 'ipInfoState', 'ipInfoRefresh']) {
        assert.equal(env.registry[id], undefined, id + ': the facts grid is gone (the status line in the tab bar carries them)');
    }
    assert.ok(env.registry.ipSnapshotBtn && env.registry.ipSnapshotQuality, 'Stream, Take picture and Picture quality are all in the panel');
    assert.equal(env.registry.ipSnapshotHint.textContent, 'ipctl.snapshot_hint_api_full ipctl.image_note', 'one muted line: what Take picture does + where the image settings are');
    assert.equal(env.registry.ipSnapshotQuality.value, 'full');
    env.api.update({ ...STATUS, fps: 14.2, resolution: '1280x720' }); // the 2 s status poll: no rebuild
    const modePanel = env.registry.ipPanelMode;
    env.api.update({ ...STATUS });
    assert.equal(env.registry.ipPanelMode, modePanel, 'the panel is not rebuilt on every status');
    env.api.update({ ...STATUS, snapshot_quality: 'small' });
    assert.equal(env.registry.ipSnapshotHint.textContent, 'ipctl.snapshot_hint_api_small ipctl.image_note');
    env.api.update({ ...STATUS, state: 'reconnecting' });
    assert.equal(env.registry.ipCameraReconnectBtn.style.display, '', 'Reconnect shows while the stream is down');

    // no zoom -> no Lens tab; no API -> no quality setting
    const plain = makeEnv({ fetchImpl: async () => json(200, { ok: true }) });
    plain.api.update({ ...STATUS, zoom_control: false, control_type: 'none' });
    assert.equal(plain.registry.cameraControlTabLens.hidden, true);
    assert.equal(plain.registry.ipSnapshotQuality, undefined);
    // no API: the hint says where the picture comes from, and that the preview has to run
    assert.equal(plain.registry.ipSnapshotHint.textContent, 'ipctl.snapshot_hint_stream_running:{"resolution":"640x360"} ipctl.image_note', 'sub stream: the real size, and to switch to Main for full resolution');
    plain.api.update({ ...STATUS, zoom_control: false, control_type: 'none', profile: 'main', resolution: '3840x2160' });
    assert.equal(plain.registry.ipSnapshotHint.textContent, 'ipctl.snapshot_hint_stream_running_main:{"resolution":"3840x2160"} ipctl.image_note');
    plain.api.update({ ...STATUS, zoom_control: false, control_type: 'none', state: 'stopped' });
    assert.equal(plain.registry.ipSnapshotHint.textContent, 'ipctl.snapshot_hint_stream_stopped ipctl.image_note');
    plain.api.update(null);
    assert.equal(plain.container.hidden, true, 'hidden for a camera that is not a network camera');
}

// UX4: snapshot mode - the stream picker is locked with a hint (the picture's size and refresh are in the status line)
{
    const env = makeEnv({ fetchImpl: async () => json(200, { ok: true }) });
    let snap = null;
    env.win.MCIpCamera.snapshotInfo = () => snap;
    env.api.update(STATUS);
    assert.equal(env.registry.ipCameraProfile.disabled, false);
    assert.equal(env.registry.ipStreamHint.hidden, true);
    snap = { reason: 'ffmpeg', width: 1920, height: 1080, intervalMs: 1000 };
    env.api.update(STATUS);
    assert.equal(env.registry.ipCameraProfile.disabled, true, 'the sub / main choice is locked');
    assert.equal(env.registry.ipStreamHint.hidden, false);
    assert.equal(env.registry.ipStreamHint.textContent, 'ipctl.stream_hint_snapshot_ffmpeg');
    snap = { reason: 'browser', width: null, height: null, intervalMs: 1000 };
    env.api.update(STATUS);
    assert.equal(env.registry.ipStreamHint.textContent, 'ipctl.stream_hint_snapshot_browser');
    snap = null;
    env.api.update(STATUS);
    assert.equal(env.registry.ipCameraProfile.disabled, false, 'a stream again: the choice is back');
}

// the Lens tab: read on open, send on release only
{
    let position = 22;
    const env = makeEnv({
        fetchImpl: async (url, options) => {
            if (url.endsWith('/control/zoom') && options.method === 'POST') { position = JSON.parse(options.body).pos; return json(200, { ok: true, position, settled: true, zoom_range: [0, 28] }); }
            if (url.endsWith('/control/zoom')) return json(200, { ok: true, zoom: { position, range: [0, 28], focus_position: 222 } });
            return json(200, { ok: true });
        },
    });
    env.api.update(STATUS);
    assert.equal(env.calls.length, 0, 'the position is NOT read until the Lens tab is opened');
    env.registry.cameraControlTabLens.fire('click');
    await tick(20);
    assert.equal(env.calls.filter((c) => c.url.endsWith('/control/zoom')).length, 1, 'read once on open');
    const slider = env.registry.ipLensSlider;
    assert.equal(slider.max, '28');
    assert.equal(slider.value, '22');
    assert.equal(slider.disabled, false);
    assert.equal(env.registry.ipLensValue.textContent, 'ipctl.lens_zoom:{"position":22,"max":28}');
    assert.equal(env.registry.ipLensFocus.textContent, 'ipctl.lens_focus:{"position":222}');
    slider.value = '25';
    slider.fire('input');
    assert.equal(env.calls.length, 1, 'dragging sends nothing');
    assert.match(env.registry.ipLensValue.textContent, /lens_zoom_target/);
    slider.fire('change'); // released
    await tick(20);
    const posts = env.calls.filter((c) => c.options.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(posts[0].options.body, '{"pos":25}');
    assert.equal(env.registry.ipLensValue.textContent, 'ipctl.lens_zoom:{"position":25,"max":28}', 'read back from the move answer');
    assert.equal(env.calls.filter((c) => c.options.method !== 'POST').length, 1, 'no extra read: the answer is the read-back');
    env.api.selectTab('mode');
    assert.equal(env.registry.ipPanelLens.hidden, true);
}

// a view-only account: the answer names the missing right and the slider stays off
{
    const env = makeEnv({
        fetchImpl: async (url, options) => (options.method === 'POST'
            ? json(403, { ok: false, error_code: 'permission_denied', right: 'lens_control' })
            : json(200, { ok: true, zoom: { position: 22, range: [0, 28], focus_position: 222 } })),
    });
    env.api.update(STATUS);
    env.registry.cameraControlTabLens.fire('click');
    await tick(20);
    env.registry.ipLensSlider.value = '24';
    env.registry.ipLensSlider.fire('change');
    await tick(20);
    assert.equal(env.registry.ipLensSlider.disabled, true);
    assert.equal(env.registry.ipLensStatus.textContent, 'ipcamera.control_denied:{"right":"ipcamera.control_right_lens_control"}');
}

// ---------------------------------------------------------------- the Snapshot button
{
    const answers = [
        json(200, { ok: true, method: 'camera_api_full', resolution: '3840x2160' }),
        json(429, { ok: false, error_code: 'rate_limited', retry_after_s: 1.2 }),
        json(507, { ok: false, error_code: 'storage_full' }),
        json(409, { ok: false, error_code: 'preview_not_running' }),
        json(200, { ok: true, method: 'stream_frame', fallback: true, resolution: '640x360' }),
        json(200, { ok: true, method: 'stream_frame', fallback: false, resolution: '640x360' }),
        json(200, { ok: true, method: 'camera_api_small', resolution: '640x360' }),
    ];
    const env = makeEnv({ fetchImpl: async () => answers.shift() });
    env.api.update(STATUS);
    const click = async () => { env.registry.ipSnapshotBtn.fire('click'); await tick(15); };
    await click();
    assert.equal(env.toasts[0][0], 'ipctl.snapshot_saved_api_full:{"resolution":"3840x2160"}', 'the toast names the real size and the source');
    assert.equal(env.win.mediaGalleryLoaded, false, 'the Media gallery is refreshed next time it opens');
    assert.equal(env.registry.ipSnapshotBtn.disabled, false);
    await click();
    assert.equal(JSON.stringify(env.toasts[1]), JSON.stringify(['ipctl.snap_err_rate_limited:{"seconds":2}', 'error']));
    await click();
    assert.equal(env.toasts[2][0], 'ipctl.snap_err_storage_full');
    await click();
    assert.equal(env.toasts[3][0], 'ipctl.snap_err_preview_not_running');
    await click();
    assert.equal(env.toasts[4][0], 'ipctl.snapshot_saved_stream:{"resolution":"640x360"}', 'the API camera that fell back to the stream says so');
    assert.equal(env.toasts[4][1], 'warning', 'a fallback is a warning, not a plain success');
    await click();
    assert.equal(env.toasts[5][0], 'ipctl.snapshot_saved_stream_only:{"resolution":"640x360"}');
    await click();
    assert.equal(env.toasts[6][0], 'ipctl.snapshot_saved_api_small:{"resolution":"640x360"}');
    assert.equal(env.calls.every((c) => c.url === '/api/camera/screenshot' && c.options.method === 'POST'), true);
}

// the quality setting goes to the settings route (not a source edit) and reverts when it fails
{
    let fail = false;
    const env = makeEnv({ fetchImpl: async () => (fail ? json(500, { ok: false, error_code: 'storage_write_failed' }) : json(200, { ok: true })) });
    env.api.update(STATUS);
    const select = env.registry.ipSnapshotQuality;
    select.value = 'small';
    select.fire('change');
    await tick(15);
    assert.equal(env.calls[0].url, '/api/camera/sources/ip%3A1/control/settings');
    assert.equal(env.calls[0].options.method, 'PUT');
    assert.equal(env.calls[0].options.body, '{"snapshot_quality":"small"}');
    env.api.update({ ...STATUS, snapshot_quality: 'small' });
    assert.equal(select.value, 'small');
    fail = true;
    select.value = 'full';
    select.fire('change');
    await tick(15);
    assert.equal(select.value, 'small', 'reverted: the save failed');
    assert.equal(env.toasts[env.toasts.length - 1][0], 'ipctl.quality_save_failed');
}

// ---------------------------------------------------------------- source rules
{
    const code = source.split(/\r?\n/).filter((line) => !line.trim().startsWith('//')).join(' ');
    assert.doesNotMatch(code, /innerHTML/, 'text only: nothing is built from strings');
    assert.doesNotMatch(code, /setInterval/, 'the lens position is never polled');
    for (const m of source.matchAll(/'(ipctl\.[a-z0-9_]+)'/g)) assert.ok(catalog.ipctl[m[1].split('.')[1]], 'i18n key exists: ' + m[1]);
    for (const loc of ['en', 'de', 'ru', 'uk']) {
        const cat = JSON.parse(readFileSync(path.join(__dirname, '..', '..', 'static', 'i18n', loc + '.json'), 'utf8'));
        for (const key of Object.keys(catalog.ipctl)) assert.ok(cat.ipctl[key], loc + ' ' + key);
        assert.ok(cat.ipctl.snap_err_rate_limited.includes('{seconds}') && cat.ipctl.lens_zoom.includes('{position}') && cat.ipctl.lens_zoom.includes('{max}'), loc + ' placeholders');
    }
}

console.log('test_camera_ip_controls.mjs: ok');
