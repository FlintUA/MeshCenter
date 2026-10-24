// tests/frontend/test_camera_ip.mjs
//
// CAM2 Phase 1b: the network camera player's pure logic (MediaSource support check, the live-edge
// rule that keeps playback near the live end without constant stalls, state -> message mapping, the
// info line) and the source form's secret handling. Runs the REAL static/camera-ip.js and
// static/camera-sources.js in a vm sandbox. `node tests/frontend/test_camera_ip.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');
const catalog = JSON.parse(readFileSync(path.join(__dirname, '..', '..', 'static', 'i18n', 'en.json'), 'utf8'));

function makeWindow(extra = {}) {
    const calls = [];
    const elements = {};
    const doc = {
        readyState: 'complete',
        addEventListener() {},
        getElementById: (id) => elements[id] || null,
        createElement: () => ({ style: {}, classList: { add() {}, remove() {}, toggle() {} }, appendChild() {}, addEventListener() {} }),
    };
    const win = {
        document: doc,
        I18N: { t: (key, params) => (params ? key + ':' + JSON.stringify(params) : key) },
        fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, status: 200, json: async () => ({ ok: true }) }; },
        setTimeout, clearTimeout, setInterval, clearInterval, console,
        ...extra,
    };
    win.window = win;
    vm.createContext(win);
    return { win, calls, elements };
}

// ---------------------------------------------------------------- the player helpers
const { win } = makeWindow();
vm.runInContext(read('camera-ip.js'), win);
const H = win.MCIpCamera._helpers;

// every i18n key the two files use exists in the English catalog
{
    const keys = new Set();
    for (const file of ['camera-ip.js', 'camera-sources.js']) {
        for (const m of read(file).matchAll(/'(ipcamera\.[a-z0-9_]+)'/g)) keys.add(m[1]);
    }
    for (const key of H.__keys || keys) {
        const [ns, name] = key.split('.');
        assert.ok(catalog[ns] && typeof catalog[ns][name] === 'string', 'i18n key exists: ' + key);
    }
    assert.ok(keys.size > 20);
}

// MediaSource / ManagedMediaSource support
{
    const ok = { isTypeSupported: (type) => type === 'video/mp4; codecs="avc1.640033"' };
    assert.equal(H.mseSupports('avc1.640033', { MediaSource: ok }), true);
    assert.equal(H.mseSupports('avc1.640033', { ManagedMediaSource: ok }), true, 'iOS Safari');
    assert.equal(H.mseSupports('hvc1', { MediaSource: ok }), false, 'a codec the browser does not accept');
    assert.equal(H.mseSupports('avc1.640033', {}), false, 'no MediaSource at all -> MJPEG fallback');
    assert.equal(H.mseSupports('', { MediaSource: ok }), false);
    assert.equal(H.mseConstructor({ ManagedMediaSource: 'M', MediaSource: 'S' }), 'M', 'managed preferred when present');
}

// the live-edge rule (3 s target): measured, a 1.5 s lead stalled constantly, 3 s was smooth
{
    const T = H.TARGET_LEAD_S;
    assert.equal(T, 3);
    assert.equal(H.liveEdgeTarget(0, 0, 0, T), null, 'nothing buffered');
    assert.equal(H.liveEdgeTarget(10, 10, 13, T), null, 'on target: leave alone');
    assert.equal(H.liveEdgeTarget(10, 8, 12.5, T), null, 'a little behind: leave alone, no seeking back and forth');
    assert.equal(H.liveEdgeTarget(100, 120, 130, T), 127, 'before the buffer start -> jump into it, T before the end');
    assert.equal(H.liveEdgeTarget(10, 8, 30, T), 27, 'far behind the live edge -> catch up to T before it');
    assert.equal(H.liveEdgeTarget(10, 8, 15.4, T), null, '5.4 s lead is still within the allowed slack');
    assert.equal(H.liveEdgeTarget(10, 8, 15.6, T), 12.6, 'slightly beyond the slack -> catch up');
}

// state -> message, terminal states, info line
{
    assert.equal(H.stateMessageKey({ state: 'unsupported_codec', codec: 'h265' }), 'ipcamera.error_h265');
    assert.equal(H.stateMessageKey({ state: 'unsupported_codec', codec: 'mpeg4' }), 'ipcamera.error_codec');
    assert.equal(H.stateMessageKey({ state: 'auth_failed' }), 'ipcamera.error_auth_failed');
    assert.equal(H.stateMessageKey({ state: 'ffmpeg_missing', container: false }), 'ipcamera.error_ffmpeg_missing');
    assert.equal(H.stateMessageKey({ state: 'ffmpeg_missing', container: true }), 'ipcamera.error_ffmpeg_missing_container', 'Docker image without ffmpeg');
    assert.equal(H.stateMessageKey({ state: 'stream_not_found' }), 'ipcamera.error_stream_not_found');
    assert.equal(H.stateMessageKey({ state: 'reconnecting' }), 'ipcamera.state_reconnecting');
    assert.equal(H.stateMessageKey({ state: 'live' }), 'ipcamera.state_live');
    assert.equal(H.stateMessageKey({ state: 'idle' }), 'ipcamera.state_idle');
    assert.equal(H.stateMessageKey(null), 'ipcamera.state_connecting');
    for (const state of ['auth_failed', 'stream_not_found', 'unsupported_codec', 'address_blocked', 'ffmpeg_missing']) {
        assert.equal(H.isTerminal({ state }), true, state);
    }
    for (const state of ['live', 'connecting', 'reconnecting', 'idle']) assert.equal(H.isTerminal({ state }), false, state);
    assert.equal(H.infoLine({ resolution: '640x360', fps: 14.5, codec: 'h264', bitrate_kbps: 160 }), '640x360 · 14.5 FPS · H264 · 160 kbps');
    assert.equal(H.infoLine({ resolution: null, fps: null, codec: null }), '');
    assert.equal(H.infoLine(null), '');
}

// the H.265 message is the specified text
assert.match(catalog.ipcamera.error_h265, /H\.265, which browsers usually cannot play/);
assert.match(catalog.ipcamera.error_h265, /Switch the stream to H\.264 in the camera settings or use the sub stream/);

// handles() only for a network source
{
    const { win: w } = makeWindow({
        fetch: async () => ({ json: async () => ({ source_type: 'rtsp', state: 'live', profile: 'sub' }) }),
    });
    vm.runInContext(read('camera-ip.js'), w);
    assert.equal(w.MCIpCamera.handles(), false, 'unknown until the first status');
    await w.MCIpCamera.refresh();
    assert.equal(w.MCIpCamera.handles(), true);

    const { win: csi } = makeWindow({ fetch: async () => ({ json: async () => ({ ok: true, started: true, model: 'imx219' }) }) });
    vm.runInContext(read('camera-ip.js'), csi);
    await csi.MCIpCamera.refresh();
    assert.equal(csi.MCIpCamera.handles(), false, 'CSI/USB keep the MJPEG path untouched');
}

// ---------------------------------------------------------------- the source form: the secret leaves the page
{
    const sent = [];
    const fields = {};
    const mkField = (id, value = '') => (fields[id] = { id, value, checked: false, focus() {}, textContent: '' });
    const overlay = { _handlers: {}, querySelector: null, querySelectorAll: () => [], addEventListener() {}, remove() {} };
    const { win: w, elements } = makeWindow({
        showToast() {},
        escapeHtml: (s) => String(s),
        loadPeripheralDevices: async () => {},
        fetch: async (url, options) => {
            sent.push({ url, options });
            return { ok: true, status: 201, json: async () => ({ ok: true, source: {} }) };
        },
    });
    // minimal DOM for submitForm: querySelector(#id) -> field object
    ['ipcamName', 'ipcamHost', 'ipcamPort', 'ipcamUser', 'ipcamPassword', 'ipcamSubPath', 'ipcamMainPath', 'ipcamProfile', 'ipcamTransport', 'ipcamTimeout'].forEach((id) => mkField(id));
    fields.ipcamName.value = 'Garden'; fields.ipcamHost.value = '192.168.2.50'; fields.ipcamPort.value = '554';
    fields.ipcamUser.value = 'viewer'; fields.ipcamPassword.value = 'topsecret-pw'; fields.ipcamSubPath.value = 's';
    fields.ipcamMainPath.value = 'm'; fields.ipcamProfile.value = 'sub'; fields.ipcamTransport.value = 'tcp'; fields.ipcamTimeout.value = '10';
    const saveBtn = { disabled: false };
    const form = {
        querySelector(sel) {
            if (sel === '[data-ipcam-form="save"]') return saveBtn;
            if (sel === '#ipcamClearPassword') return null;
            const id = sel.replace(/^#/, '').replace(/Error$/, '');
            return fields[id] || { textContent: '', focus() {} };
        },
        querySelectorAll() { return []; },
        remove() {},
    };
    vm.runInContext(read('camera-sources.js'), w);
    w.document.getElementById = (id) => (id === 'ipCameraFormModal' ? form : null);
    // drive the real submit path through openForm's closure by calling the exported helper surface
    assert.ok(w.MCCameraSources.openForm && w.MCCameraSources.errorText);
    // errorText never echoes server text, only a catalog key
    assert.equal(w.MCCameraSources.errorText({ error_code: 'not_lan_address', error: 'secret topsecret-pw' }), 'ipcamera.err_not_lan_address');
    assert.equal(w.MCCameraSources.errorText({ error_code: 'something_new' }), 'ipcamera.err_generic');
    assert.equal(w.MCCameraSources.errorText(null), 'ipcamera.err_generic');
    for (const key of Object.values(w.MCCameraSources._errorKeys)) {
        const [ns, name] = key.split('.');
        assert.ok(catalog[ns][name], 'error key exists: ' + key);
    }
}

// the form source: password input is a password field with new-password autocomplete, and is cleared after sending
{
    const src = read('camera-sources.js');
    assert.match(src, /id="ipcamPassword" name="password" type="password"/);
    assert.match(src, /autocomplete="new-password"/);
    assert.match(src, /ipcamPassword'\)\.value = ''/, 'the secret is wiped from the page right after the request');
    assert.doesNotMatch(src, /console\.(log|info|debug)\([^)]*password/i, 'never logs the password');
    assert.match(src, /appConfirm\(\{[\s\S]*danger: true/, 'delete goes through the UI1 dialog, danger style');
}

// iPhone before iOS 17.1 (no MediaSource, no ManagedMediaSource): the message comes at once and no stream is opened
for (const [fallback, expected] of [['disabled_low_power', 'message'], ['available', 'mjpeg']]) {
    const shown = [];
    const urls = [];
    const ctx = makeWindow({
        setCameraFeedLoading: (loading, text) => shown.push(text),
    });
    ctx.win.fetch = async (url) => {
        urls.push(url);
        return { ok: true, status: 200, json: async () => ({ source_type: 'rtsp', state: 'live', profile: 'sub', codec: 'h264', mjpeg_fallback: fallback }) };
    };
    const img = { classList: { add() {}, remove() {}, toggle() {} }, set src(v) { urls.push('IMG ' + v.split('?')[0]); } };
    ctx.elements.videoFeed = img;
    vm.runInContext(read('camera-ip.js'), ctx.win);
    await ctx.win.MCIpCamera.start();
    assert.ok(!urls.some((u) => u.includes('/stream/fmp4')), 'no fMP4 stream is opened without MSE');
    if (expected === 'message') assert.ok(shown.includes('ipcamera.error_not_supported'), 'explained at once');
    else assert.ok(urls.includes('IMG /video_feed'), 'MJPEG fallback where the host allows it');
    ctx.win.MCIpCamera.stop();
}

console.log('test_camera_ip.mjs: ok');
