// tests/frontend/test_camera_csrf.mjs
//
// CSRF1: the camera source card on top of the REAL shared static/csrf.js. A first 403 csrf_invalid is
// recovered transparently (token refresh + one replay), a persistent one shows the translated message and the
// shared Reload prompt, and any other failure names the real error code instead of "could not be saved".
// `node tests/frontend/test_camera_csrf.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');

function response(status, body) {
    return { status, ok: status >= 200 && status < 300, json: async () => body, clone() { return response(status, body); } };
}

function load(handler) {
    const log = [];
    const notifications = [];
    const warnings = [];
    let token = 'stale';
    const meta = { getAttribute: () => token, setAttribute(name, value) { if (name === 'content') token = value; } };
    const window = {
        location: { href: 'http://mc.test/', origin: 'http://mc.test', reload() {} },
        document: { querySelector: () => meta, getElementById: () => null, body: { appendChild() {} }, createElement: () => ({ setAttribute() {}, style: {} }) },
        I18N: { t: (k, p) => (p ? k + ':' + JSON.stringify(p) : k), tOrFallback: (k, p, f) => f },
        addNotification: (message, kind, options) => notifications.push({ message, kind, persistent: options && options.persistent }),
        console: { warn: (...a) => warnings.push(a.join(' ')), log() {} },
        setTimeout, clearTimeout, Promise, URL, Headers, Request, Response,
    };
    window.document = window.document;
    window.window = window;
    window.fetch = async (url, options) => {
        const headers = new Headers(options && options.headers);
        log.push({ url: String(url), method: (options && options.method) || 'GET', token: headers.get('x-csrf-token') });
        return handler(String(url), options, log);
    };
    vm.createContext(window);
    vm.runInContext(read('csrf.js'), window);
    vm.runInContext(read('camera-sources.js'), window);
    return { window, log, notifications, warnings };
}

// the first call is rejected (stale page token), the shared wrapper refreshes and replays once: the card sees success
{
    let first = true;
    const { window, log, notifications } = load((url) => {
        if (url === '/api/auth/csrf') return response(200, { ok: true, csrf_token: 'fresh' });
        if (first) { first = false; return response(403, { ok: false, error_code: 'csrf_invalid' }); }
        return response(200, { ok: true });
    });
    const result = await window.MCCameraSources._request('POST', '/api/camera/sources', { name: 'x' });
    assert.equal(result.ok, true, 'recovered without the user seeing an error');
    assert.deepEqual(log.map((c) => c.url), ['/api/camera/sources', '/api/auth/csrf', '/api/camera/sources']);
    assert.equal(log[0].token, 'stale');
    assert.equal(log[2].token, 'fresh', 'the replay carries the refreshed token');
    assert.equal(notifications.length, 0);
}

// still rejected after the refresh: translated message, the shared Reload prompt (once), a console line without the body
{
    const { window, notifications, warnings } = load((url) => {
        if (url === '/api/auth/csrf') return response(200, { ok: true, csrf_token: 'fresh' });
        return response(403, { ok: false, error_code: 'csrf_invalid', secret: 'hunter2' });
    });
    const result = await window.MCCameraSources._request('PUT', '/api/camera/sources/abc', { password: 'hunter2' });
    assert.equal(result.ok, false);
    assert.equal(result.status, 403);
    assert.equal(window.MCCameraSources.errorText(result.body, result.status), 'errors.csrf_invalid');
    assert.equal(notifications.length, 1, 'the shared reload prompt, once');
    assert.equal(notifications[0].persistent, true);
    assert.ok(warnings.some((w) => w.includes('HTTP 403') && w.includes('csrf_invalid') && w.includes('/api/camera/sources/abc')), 'logged with path and code');
    assert.ok(!warnings.join('\n').includes('hunter2'), 'the body and the password are never logged');
}

// any other failure names its code and status
{
    const { window } = load(() => response(500, { ok: false, error_code: 'storage_broken' }));
    const result = await window.MCCameraSources._request('POST', '/api/camera/sources', {});
    assert.equal(window.MCCameraSources.errorText(result.body, result.status), 'ipcamera.err_generic_code:{"code":"storage_broken","status":500}');
}

console.log('test_camera_csrf.mjs: ok');
