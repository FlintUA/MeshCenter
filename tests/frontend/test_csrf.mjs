// tests/frontend/test_csrf.mjs
//
// Step 1.6A.0: a minimal, dependency-free behavior test for static/csrf.js
// — the project-wide X-CSRF-Token fetch wrapper. Like the sibling
// test_updates_blocked_ui.mjs, this project has no build step / package.json
// / JS test framework (CI's JS coverage is `node --check`, syntax only), so
// rather than introduce jest or a browser runner, this uses Node's built-in
// `vm` to execute the REAL source against a small stub window/document/fetch
// and Node's built-in `assert`, plus Node's REAL Request/Headers/FormData
// implementations. No npm install. Run with:
//
//   node tests/frontend/test_csrf.mjs
//
// What this proves, against the REAL csrf.js (not a reimplementation): the
// token is read from the meta tag and added only to same-origin /api/
// requests whose method is anything other than GET/HEAD; GET/HEAD/cross-origin
// /non-API calls are untouched; a Request input's own headers are inherited
// (and init.headers overrides them, per fetch semantics) without mutating the
// Request or the caller's objects; a pre-existing x-csrf-token is replaced,
// not duplicated; FormData passes through with no forced Content-Type; and a
// 403 csrf_invalid response triggers exactly one persistent reload prompt
// (for a same-origin mutating /api/ call, even with a missing token, and
// never for a cross-origin or safe call) with no retry and no body
// consumption, while every other status passes through.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(__dirname, '..', '..', 'static', 'csrf.js');
const source = readFileSync(scriptPath, 'utf8');

function fakeResponse(status, body) {
    return {
        status,
        ok: status >= 200 && status < 300,
        json: async () => body,
        // Each clone() returns a fresh object so the wrapper's private peek
        // and the caller's own json() read independent copies of the body.
        clone() { return fakeResponse(status, body); },
    };
}

function loadCSRF({ token = 'tok123', origin = 'http://localhost:5000', fetchImpl } = {}) {
    const fetchLog = [];
    const originalFetch = async (url, options) => {
        fetchLog.push({ url, options });
        if (fetchImpl) return fetchImpl(url, options);
        return fakeResponse(200, { ok: true });
    };

    const meta = {
        getAttribute: (name) => (name === 'content' ? token : null),
    };
    const document = {
        querySelector: (selector) => (selector === 'meta[name="csrf-token"]' ? meta : null),
        body: { appendChild: () => {} },
        createElement: () => ({ setAttribute() {}, textContent: '', style: {} }),
    };

    const window = {
        location: { href: `${origin}/`, origin, reload() {} },
        fetch: originalFetch,
        I18N: {
            tOrFallback: (key, params, fallback) => fallback,
        },
        // addNotification is attached by individual tests after load, because
        // csrf.js only checks for it at call time.
    };

    const sandbox = {
        console,
        window,
        document,
        URL,
        Headers,
        Object,
        Array,
        String,
        Promise,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'csrf.js' });
    return { sandbox, window, document, fetchLog };
}

async function test_needsToken_classification() {
    const { window } = loadCSRF();
    const n = window.MeshCenterCSRF.needsToken;

    // Any method other than GET/HEAD on same-origin /api/ paths needs the
    // token — the same rule as the backend, so it can't drift.
    assert.equal(n('/api/x', { method: 'POST' }), true);
    assert.equal(n('/api/x', { method: 'PUT' }), true);
    assert.equal(n('/api/x', { method: 'PATCH' }), true);
    assert.equal(n('/api/x', { method: 'DELETE' }), true);
    assert.equal(n('/api/x', { method: 'OPTIONS' }), true, 'OPTIONS is unsafe');
    assert.equal(n('/api/x', { method: 'PURGE' }), true, 'unknown future method is unsafe');
    assert.equal(n('/api/x', { method: 'post' }), true, 'method is case-insensitive');

    // Request-like input (url + method, no separate init).
    assert.equal(n({ url: '/api/x', method: 'POST' }, undefined), true);

    // Safe methods and defaults are exempt.
    assert.equal(n('/api/x', { method: 'GET' }), false);
    assert.equal(n('/api/x', { method: 'HEAD' }), false);
    assert.equal(n('/api/x', {}), false, 'no method defaults to GET');
    assert.equal(n({ url: '/api/x', method: 'GET' }, undefined), false);

    // Non-/api/ paths and cross-origin URLs are out of scope.
    assert.equal(n('/not-api/x', { method: 'POST' }), false);
    assert.equal(n('http://evil.example/api/x', { method: 'POST' }), false);
    assert.equal(n('https://localhost:5000/api/x', { method: 'POST' }), false, 'scheme change is cross-origin');
    assert.equal(n('http://localhost:9000/api/x', { method: 'POST' }), false, 'port change is cross-origin');
    assert.equal(n('not-a-url', { method: 'POST' }), false);

    console.log('PASS: test_needsToken_classification');
}

async function test_addTokenHeader_preserves_and_does_not_mutate() {
    const { window } = loadCSRF();
    const add = window.MeshCenterCSRF.addTokenHeader;

    // Plain-object headers: caller's object is untouched, result is a copy.
    const init = {
        method: 'POST',
        body: 'abc',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
    };
    const originalHeaders = init.headers;
    const next = add(undefined, init, 'tok123');
    assert.equal(init.headers, originalHeaders);
    assert.equal(init.headers['X-CSRF-Token'], undefined, 'caller headers must not be mutated');
    assert.equal(next.headers.get('X-CSRF-Token'), 'tok123');
    assert.equal(next.headers.get('Content-Type'), 'application/json');
    assert.equal(next.body, 'abc');
    assert.equal(next.method, 'POST');
    assert.equal(next.credentials, 'same-origin');
    assert.notEqual(next, init, 'must return a copy, not the same object');

    // Array-of-pairs headers.
    const arrInit = { method: 'POST', headers: [['A', '1']] };
    const arrNext = add(undefined, arrInit, 'tok');
    assert.equal(arrInit.headers.length, 1, 'caller array must not be mutated');
    assert.equal(arrInit.headers[0][0], 'A');
    assert.equal(arrInit.headers[0][1], '1');
    assert.equal(arrNext.headers.get('A'), '1');
    assert.equal(arrNext.headers.get('X-CSRF-Token'), 'tok');

    // Headers instance (Node's real Headers) when available.
    if (typeof Headers !== 'undefined') {
        const instance = new Headers({ 'X-A': 'b' });
        const hInit = { method: 'POST', headers: instance };
        const hNext = add(undefined, hInit, 'tok');
        assert.equal(instance.get('X-CSRF-Token'), null, 'caller Headers instance must not be mutated');
        assert.equal(hNext.headers.get('X-CSRF-Token'), 'tok');
        assert.equal(hNext.headers.get('X-A'), 'b');
    }

    // No headers at all.
    const bare = add(undefined, { method: 'POST' }, 'tok');
    assert.equal(bare.headers.get('X-CSRF-Token'), 'tok');

    console.log('PASS: test_addTokenHeader_preserves_and_does_not_mutate');
}

async function test_addTokenHeader_inherits_request_headers() {
    const { window } = loadCSRF();
    const add = window.MeshCenterCSRF.addTokenHeader;

    // A real Request input with no init.headers: its headers must be copied
    // (not discarded) and the CSRF header added, without mutating the Request.
    const request = new Request('http://localhost:5000/api/thing', {
        method: 'POST',
        headers: { Accept: 'application/json', 'X-Custom': 'y' },
    });
    const next = add(request, undefined, 'tok123');

    assert.equal(request.headers.get('X-CSRF-Token'), null, 'Request headers must not be mutated');
    assert.equal(next.headers.get('Accept'), 'application/json');
    assert.equal(next.headers.get('X-Custom'), 'y');
    assert.equal(next.headers.get('X-CSRF-Token'), 'tok123');

    console.log('PASS: test_addTokenHeader_inherits_request_headers');
}

async function test_addTokenHeader_init_headers_override_request() {
    const { window } = loadCSRF();
    const add = window.MeshCenterCSRF.addTokenHeader;

    // Per fetch semantics, an explicit init.headers REPLACES a Request's own
    // headers entirely — but still gains the CSRF header.
    const request = new Request('http://localhost:5000/api/thing', {
        method: 'POST',
        headers: { Accept: 'application/json' },
    });
    const next = add(request, { headers: { 'Content-Type': 'text/plain' } }, 'tok123');

    assert.equal(next.headers.get('Content-Type'), 'text/plain');
    assert.equal(next.headers.get('Accept'), null, 'Request headers must be replaced, not merged');
    assert.equal(next.headers.get('X-CSRF-Token'), 'tok123');

    console.log('PASS: test_addTokenHeader_init_headers_override_request');
}

async function test_addTokenHeader_lowercase_csrf_replaced_not_duplicated() {
    const { window } = loadCSRF();
    const add = window.MeshCenterCSRF.addTokenHeader;

    // A caller-supplied x-csrf-token (lowercase) must be replaced by our
    // token, case-insensitively, not left beside a duplicate X-CSRF-Token.
    const next = add(undefined, { headers: { 'x-csrf-token': 'old' } }, 'new');
    assert.equal(next.headers.get('x-csrf-token'), 'new');
    assert.equal(next.headers.get('X-CSRF-Token'), 'new');

    let csrfCount = 0;
    for (const [name] of next.headers.entries()) {
        if (name.toLowerCase() === 'x-csrf-token') csrfCount += 1;
    }
    assert.equal(csrfCount, 1, 'must be a single CSRF header, not duplicated');

    console.log('PASS: test_addTokenHeader_lowercase_csrf_replaced_not_duplicated');
}

async function test_fetch_adds_header_to_unsafe_same_origin_api() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    await window.fetch('/api/thing', { method: 'POST', body: 'x' });
    assert.equal(fetchLog.length, 1);
    const { url, options } = fetchLog[0];
    assert.equal(url, '/api/thing');
    assert.equal(options.headers.get('X-CSRF-Token'), 'tok123');
    assert.equal(options.body, 'x', 'caller body must be preserved');
    console.log('PASS: test_fetch_adds_header_to_unsafe_same_origin_api');
}

async function test_fetch_options_method_receives_token() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    await window.fetch('/api/x', { method: 'OPTIONS' });
    assert.equal(fetchLog[0].options.headers.get('X-CSRF-Token'), 'tok123');
    console.log('PASS: test_fetch_options_method_receives_token');
}

async function test_fetch_preserves_request_headers_and_body() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    const request = new Request('http://localhost:5000/api/thing', {
        method: 'POST',
        headers: { Accept: 'application/json', 'X-Custom': 'y' },
        body: 'the-body',
    });

    await window.fetch(request);

    assert.equal(fetchLog.length, 1);
    const { url, options } = fetchLog[0];
    assert.equal(url, request, 'the Request object must be passed through unchanged');
    assert.equal(options.headers.get('Accept'), 'application/json', 'Request headers preserved');
    assert.equal(options.headers.get('X-Custom'), 'y');
    assert.equal(options.headers.get('X-CSRF-Token'), 'tok123');
    assert.equal(request.headers.get('X-CSRF-Token'), null, 'Request must not be mutated');
    assert.equal(await request.text(), 'the-body', 'Request body must be preserved');

    console.log('PASS: test_fetch_preserves_request_headers_and_body');
}

async function test_fetch_formdata_no_content_type_forced() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    const formData = new FormData();
    formData.append('file', new Blob(['abc'], { type: 'text/plain' }), 'a.txt');

    await window.fetch('/api/upload', { method: 'POST', body: formData });

    const { options } = fetchLog[0];
    assert.equal(options.body, formData, 'the FormData body must pass through unchanged');
    assert.equal(options.headers.get('content-type'), null, 'no Content-Type may be forced for FormData');
    assert.equal(options.headers.get('X-CSRF-Token'), 'tok123');

    console.log('PASS: test_fetch_formdata_no_content_type_forced');
}

async function test_fetch_skips_header_for_get_and_cross_origin() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    await window.fetch('/api/thing'); // GET
    await window.fetch('http://evil.example/api/x', { method: 'POST' });
    await window.fetch('/some/page', { method: 'POST' });

    assert.equal(fetchLog.length, 3);
    assert.equal(fetchLog[0].options && fetchLog[0].options.headers, undefined, 'GET must not gain a header');
    assert.equal(fetchLog[1].options && fetchLog[1].options.headers, undefined, 'cross-origin must not gain a header');
    assert.equal(fetchLog[2].options && fetchLog[2].options.headers, undefined, 'non-/api/ path must not gain a header');
    console.log('PASS: test_fetch_skips_header_for_get_and_cross_origin');
}

async function test_no_token_in_meta_means_no_header_added() {
    const { window, fetchLog } = loadCSRF({ token: '' });
    await window.fetch('/api/x', { method: 'POST', headers: { 'X-Custom': 'y' } });
    assert.equal(fetchLog.length, 1);
    // With no token, addTokenHeader is never called, so the caller's own
    // headers object passes through untouched (a plain object, not a Headers
    // instance) — still the caller's object, still no CSRF header added.
    assert.equal(fetchLog[0].options.headers['X-Custom'], 'y', 'caller header preserved');
    assert.equal(fetchLog[0].options.headers['X-CSRF-Token'], undefined);
    console.log('PASS: test_no_token_in_meta_means_no_header_added');
}

async function test_csrf_invalid_403_shows_prompt_without_retry() {
    const { window, fetchLog } = loadCSRF({
        token: 'tok123',
        fetchImpl: async () => fakeResponse(403, { ok: false, error_code: 'csrf_invalid' }),
    });
    let notified = null;
    window.addNotification = (msg, type, opts) => { notified = { msg, type, opts }; };

    const response = await window.fetch('/api/x', { method: 'POST' });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.ok(notified, 'a csrf_invalid 403 must trigger the reload prompt');
    assert.equal(notified.type, 'error');
    assert.equal(notified.opts.persistent, true, 'the prompt must be persistent');
    assert.equal(typeof notified.opts.action, 'function', 'the prompt must carry a reload action');
    assert.equal(response.status, 403);
    assert.equal(fetchLog.length, 1, 'must never auto-retry');
    assert.equal((await response.json()).error_code, 'csrf_invalid', 'caller can still read the body');
    console.log('PASS: test_csrf_invalid_403_shows_prompt_without_retry');
}

async function test_missing_token_403_still_shows_prompt() {
    // A missing/stale page token is exactly when the reload instruction is
    // needed — the 403 inspection must fire even though no header was added.
    const { window, fetchLog } = loadCSRF({
        token: '',
        fetchImpl: async () => fakeResponse(403, { ok: false, error_code: 'csrf_invalid' }),
    });
    let notified = null;
    window.addNotification = (msg, type, opts) => { notified = { msg, type, opts }; };

    const response = await window.fetch('/api/x', { method: 'POST' });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.ok(notified, 'a same-origin unsafe /api/ 403 must prompt even without a page token');
    assert.equal(response.status, 403);
    assert.equal(fetchLog.length, 1, 'must never auto-retry');
    console.log('PASS: test_missing_token_403_still_shows_prompt');
}

async function test_cross_origin_403_csrf_invalid_does_not_prompt() {
    const { window } = loadCSRF({
        token: 'tok123',
        fetchImpl: async () => fakeResponse(403, { ok: false, error_code: 'csrf_invalid' }),
    });
    let notified = null;
    window.addNotification = (msg, type, opts) => { notified = { msg, type, opts }; };

    const response = await window.fetch('http://evil.example/api/x', { method: 'POST' });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(notified, null, 'a cross-origin 403 must not show the MeshCenter reload prompt');
    assert.equal(response.status, 403);
    console.log('PASS: test_cross_origin_403_csrf_invalid_does_not_prompt');
}

async function test_concurrent_csrf_invalid_produce_exactly_one_prompt() {
    const { window, fetchLog } = loadCSRF({
        token: 'tok123',
        fetchImpl: async () => fakeResponse(403, { ok: false, error_code: 'csrf_invalid' }),
    });
    let notifyCount = 0;
    window.addNotification = () => { notifyCount += 1; };

    await Promise.all([
        window.fetch('/api/a', { method: 'POST' }),
        window.fetch('/api/b', { method: 'POST' }),
        window.fetch('/api/c', { method: 'POST' }),
    ]);
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(notifyCount, 1, 'several concurrent failures must produce exactly one prompt');
    assert.equal(fetchLog.length, 3);
    console.log('PASS: test_concurrent_csrf_invalid_produce_exactly_one_prompt');
}

async function test_non_csrf_error_passes_through_without_prompt() {
    const { window, fetchLog } = loadCSRF({
        token: 'tok123',
        fetchImpl: async () => fakeResponse(403, { ok: false, error_code: 'forbidden' }),
    });
    let notified = null;
    window.addNotification = (msg, type, opts) => { notified = { msg, type, opts }; };

    const response = await window.fetch('/api/x', { method: 'POST' });
    await new Promise((resolve) => setTimeout(resolve, 0));

    assert.equal(notified, null, 'a non-csrf 403 must not trigger the reload prompt');
    assert.equal(response.status, 403);
    assert.equal(fetchLog.length, 1);
    console.log('PASS: test_non_csrf_error_passes_through_without_prompt');
}

async function main() {
    await test_needsToken_classification();
    await test_addTokenHeader_preserves_and_does_not_mutate();
    await test_addTokenHeader_inherits_request_headers();
    await test_addTokenHeader_init_headers_override_request();
    await test_addTokenHeader_lowercase_csrf_replaced_not_duplicated();
    await test_fetch_adds_header_to_unsafe_same_origin_api();
    await test_fetch_options_method_receives_token();
    await test_fetch_preserves_request_headers_and_body();
    await test_fetch_formdata_no_content_type_forced();
    await test_fetch_skips_header_for_get_and_cross_origin();
    await test_no_token_in_meta_means_no_header_added();
    await test_csrf_invalid_403_shows_prompt_without_retry();
    await test_missing_token_403_still_shows_prompt();
    await test_cross_origin_403_csrf_invalid_does_not_prompt();
    await test_concurrent_csrf_invalid_produce_exactly_one_prompt();
    await test_non_csrf_error_passes_through_without_prompt();
    console.log('All CSRF frontend tests passed.');
}

main().catch((error) => {
    console.error('FAIL:', error);
    process.exitCode = 1;
});
