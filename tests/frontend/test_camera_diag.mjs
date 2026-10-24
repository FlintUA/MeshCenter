// tests/frontend/test_camera_diag.mjs
//
// CAM2-M: the ?camdiag=1 report. It must (1) never contain an address, URL or credential, (2) describe the
// browser's capabilities, (3) tell apart "sourceopen never fires unless video.load() is called" from a working
// browser (the leading suspect for Safari on iPhone). Runs the REAL static/camera-diag.js in a vm sandbox
// with fake MediaSource objects. `node tests/frontend/test_camera_diag.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');
const catalog = JSON.parse(readFileSync(path.join(__dirname, '..', '..', 'static', 'i18n', 'en.json'), 'utf8'));
const source = read('camera-diag.js');

class Target {
    constructor() { this.l = {}; }
    addEventListener(name, fn, opts) { (this.l[name] = this.l[name] || []).push({ fn, once: Boolean(opts && opts.once) }); }
    removeEventListener() {}
    emit(name) {
        for (const entry of [...(this.l[name] || [])]) {
            entry.fn({ type: name });
            if (entry.once) this.l[name] = this.l[name].filter((e) => e !== entry);
        }
    }
}

function makeWindow({ managed = false, plain = false, openOnlyAfterLoad = false, fetchStatus, userAgent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1' } = {}) {
    const timeline = { sourceAttached: 0, loadCalled: 0 };
    let lastMs = null;
    const mkCtor = () => class extends Target {
        constructor() { super(); this.readyState = 'closed'; this.streaming = true; this.buffers = []; lastMs = this; }
        addSourceBuffer() {
            const b = new Target();
            b.buffered = { length: 0 };
            b.appendBuffer = () => setTimeout(() => b.emit('updateend'), 1);
            return b;
        }
        static isTypeSupported(mime) { return /avc1\.(640033|64001f)/.test(mime); }
    };
    const win = {
        navigator: { userAgent, platform: 'iPhone', maxTouchPoints: 5 },
        screen: { width: 390, height: 844 }, devicePixelRatio: 3,
        ReadableStream: function ReadableStream() {}, AbortController,
        URL: { createObjectURL: () => 'blob:fake', revokeObjectURL() {} },
        I18N: { t: (k) => k },
    };
    if (managed) win.ManagedMediaSource = mkCtor();
    if (plain) win.MediaSource = mkCtor();
    const doc = {
        body: { appendChild() {} },
        createElement(tag) {
            const el = new Target();
            el.style = {}; el.attrs = {}; el.children = [];
            el.setAttribute = (k, v) => { el.attrs[k] = v; };
            el.appendChild = (c) => {
                el.children.push(c);
                timeline.sourceAttached += 1;
                // a <source> appended to an idle element does NOT start loading in this fake (the suspected Safari behaviour)
                if (!openOnlyAfterLoad) setTimeout(() => lastMs.emit('sourceopen'), 1);
            };
            el.load = () => { timeline.loadCalled += 1; if (openOnlyAfterLoad) setTimeout(() => lastMs.emit('sourceopen'), 1); };
            // video.src = blobUrl starts loading at once in every engine
            Object.defineProperty(el, 'src', { set() { if (tag === 'video') setTimeout(() => lastMs && lastMs.emit('sourceopen'), 1); }, get() { return ''; } });
            el.remove = () => {};
            el.play = () => Promise.resolve();
            el.paused = false; el.readyState = 4; el.networkState = 2; el.currentTime = 0.1; el.videoWidth = 640;
            if (tag === 'video') setTimeout(() => el.emit('playing'), 30);
            return el;
        },
    };
    win.document = doc;
    // the MediaSource instance each attempt creates is tracked by the constructor itself
    win.fetch = async (url) => {
        if (url.includes('/status')) return { ok: true, json: async () => fetchStatus };
        const chunk = new Uint8Array([0, 0, 0, 12, 0x66, 0x74, 0x79, 0x70, 1, 2, 3, 4]);
        let sent = 0;
        return { ok: true, status: 200, body: { getReader: () => ({ read: async () => (sent++ < 6 ? { value: chunk, done: false } : { done: true }) }) } };
    };
    win.performance = performance;
    win.setTimeout = setTimeout;
    return { win, timeline };
}

function load(win) {
    const ctx = { window: win, document: win.document, navigator: win.navigator, performance, setTimeout, clearTimeout, console, AbortController, URLSearchParams, Promise };
    vm.createContext(ctx);
    ctx.window.location = { search: '' };
    ctx.window.setTimeout = setTimeout;
    vm.runInContext(source, ctx);
    return ctx.window.MCCameraDiag;
}

// ---------------------------------------------------------------- privacy: nothing address-like leaves the report
{
    const { win } = makeWindow({ managed: true });
    const D = load(win);
    const dirty = 'see rtsp://user:secret@192.168.2.114:554/Preview_01_sub and http://cam.lan/x host 10.0.0.5 and fe80:0:0:0:1:2:3:4 mail a@b.c';
    const clean = D.sanitize(dirty);
    for (const bad of ['rtsp://', 'http://', 'secret', '192.168', '10.0.0.5', 'fe80', 'a@b.c', 'user:']) assert.ok(!clean.includes(bad), 'removed: ' + bad);
    assert.equal(D.sanitize('Version/17.4 Mobile/15E148 Safari/604.1 605.1.15'), 'Version/17.4 Mobile/15E148 Safari/604.1 605.1.15', 'a user agent is left alone');
}

// the source never reads the page address; only the ?camdiag flag from location.search
{
    assert.doesNotMatch(source, /location\.(href|host|hostname|origin|pathname|hash)/);
    assert.doesNotMatch(source, /document\.cookie|localStorage|sessionStorage/);
    assert.match(source, /location\.search/);
}

// every i18n key it uses exists
{
    for (const m of source.matchAll(/t\('(camdiag\.[a-z]+)'\)/g)) {
        const [ns, name] = m[1].split('.');
        assert.equal(typeof catalog[ns][name], 'string', m[1]);
    }
}

// ---------------------------------------------------------------- capability report
{
    const iphone = makeWindow({ managed: true });
    const lines = load(iphone.win).capabilityLines(iphone.win, 'avc1.640033').join('\n');
    assert.match(lines, /MediaSource: NO, ManagedMediaSource: yes/);
    assert.match(lines, /player picks: ManagedMediaSource/);
    assert.match(lines, /ManagedMediaSource avc1\.640033: true/);
    assert.match(lines, /ManagedMediaSource avc1\.4d401f: false/);
    const none = makeWindow({});
    assert.match(load(none.win).capabilityLines(none.win, null).join('\n'), /none \(capability check fails\)/);
    const desktop = makeWindow({ plain: true, managed: true });
    assert.match(load(desktop.win).capabilityLines(desktop.win, 'avc1.640033').join('\n'), /MediaSource: yes, ManagedMediaSource: yes/);
}

const rtspStatus = { source_type: 'rtsp', state: 'live', profile: 'sub', codec: 'h264', mse_codec: 'avc1.640033', resolution: '640x360', fps: 14, mjpeg_fallback: 'disabled_low_power', container: false,
    host: '192.168.2.114', url: 'rtsp://u:p@192.168.2.114/x' };

// not an IP camera / no codec yet / no MSE at all: explained, no attempts
{
    let ctx = makeWindow({ managed: true, fetchStatus: { source_type: null, state: undefined } });
    assert.match(await load(ctx.win).run(ctx.win), /no network camera is active/);
    ctx = makeWindow({ managed: true, fetchStatus: { ...rtspStatus, mse_codec: null } });
    assert.match(await load(ctx.win).run(ctx.win), /no codec yet/);
    ctx = makeWindow({ fetchStatus: rtspStatus });
    const text = await load(ctx.win).run(ctx.win);
    assert.match(text, /neither MediaSource nor ManagedMediaSource/);
    assert.ok(!text.includes('192.168') && !text.includes('rtsp://'), 'status fields outside the whitelist are never copied');
}

// a browser where the source is only opened by video.load() (the suspected Safari behaviour): attempt 1 FAILs, attempt 2 PASSes
{
    const ctx = makeWindow({ managed: true, openOnlyAfterLoad: true, fetchStatus: rtspStatus });
    const text = await load(ctx.win).run(ctx.win, { timeoutScale: 0.02 });
    const parts = text.split('attempt "');
    assert.equal(parts.length, 3, 'two managed attempts, no plain MediaSource here');
    assert.match(parts[1], /managed, as the player does today/);
    assert.match(parts[1], /RESULT: FAIL, sourceopen never fired/);
    assert.match(parts[2], /managed, with video\.load\(\)/);
    assert.match(parts[2], /video\.load\(\) called/);
    assert.match(parts[2], /RESULT: PASS/);
    assert.ok(!text.includes('192.168') && !text.includes('rtsp://') && !text.includes('http'), 'no address, URL or credential in the report');
    assert.match(text, /server: source_type=rtsp, state=live, profile=sub, codec=h264, mse_codec=avc1\.640033/);
    assert.match(text, /first chunk: 12 bytes, boxes ftyp\(12\)/);
}

// a working desktop browser: everything passes
{
    const ctx = makeWindow({ plain: true, fetchStatus: rtspStatus });
    const text = await load(ctx.win).run(ctx.win, { timeoutScale: 0.02 });
    assert.match(text, /plain MediaSource, video\.src/);
    assert.match(text, /RESULT: PASS/);
}

console.log('test_camera_diag.mjs: ok');
