// tests/frontend/test_csrf.mjs
//
// Step 1.6A.0: a minimal, dependency-free behavior test for static/csrf.js
// — the project-wide X-CSRF-Token fetch wrapper. Like the sibling
// test_updates_blocked_ui.mjs, this project has no build step / package.json
// / JS test framework (CI's JS coverage is `node --check`, syntax only), so
// rather than introduce jest or a browser runner, this uses Node's built-in
// `vm` to execute the REAL source against a small stub window/document/fetch
// and Node's built-in `assert`. No npm install. Run with:
//
//   node tests/frontend/test_csrf.mjs
//
// What this proves, against the REAL csrf.js (not a reimplementation): the
// token is read from the meta tag and added only to same-origin /api/
// requests with a mutating method; GET/HEAD/cross-origin/non-API calls are
// untouched; the caller's init/Headers are never mutated; and a 403
// csrf_invalid response triggers exactly one persistent reload prompt with no
// retry and no body consumption, while every other status passes through.

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

    // Mutating methods on same-origin /api/ paths require the token.
    assert.equal(n('/api/x', { method: 'POST' }), true);
    assert.equal(n('/api/x', { method: 'PUT' }), true);
    assert.equal(n('/api/x', { method: 'PATCH' }), true);
    assert.equal(n('/api/x', { method: 'DELETE' }), true);
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
    assert.equal(n('http://localhost:5000:9000/api/x', { method: 'POST' }), false, 'port change is cross-origin');
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
    const next = add(init, 'tok123');
    assert.equal(init.headers, originalHeaders);
    assert.equal(init.headers['X-CSRF-Token'], undefined, 'caller headers must not be mutated');
    assert.equal(next.headers['X-CSRF-Token'], 'tok123');
    assert.equal(next.headers['Content-Type'], 'application/json');
    assert.equal(next.body, 'abc');
    assert.equal(next.method, 'POST');
    assert.equal(next.credentials, 'same-origin');
    assert.notEqual(next, init, 'must return a copy, not the same object');

    // Array-of-pairs headers. Compared element-wise: the pushed pair is a
    // vm-realm array, so deepStrictEqual would reject it on prototype
    // identity even though its contents match (a test-harness artifact, not
    // a code defect - in a browser everything shares one realm).
    const arrInit = { method: 'POST', headers: [['A', '1']] };
    const arrNext = add(arrInit, 'tok');
    assert.equal(arrInit.headers.length, 1, 'caller array must not be mutated');
    assert.equal(arrInit.headers[0][0], 'A');
    assert.equal(arrInit.headers[0][1], '1');
    assert.equal(arrNext.headers.length, 2);
    assert.equal(arrNext.headers[0][0], 'A');
    assert.equal(arrNext.headers[0][1], '1');
    assert.equal(arrNext.headers[1][0], 'X-CSRF-Token');
    assert.equal(arrNext.headers[1][1], 'tok');

    // Headers instance (Node's real Headers) when available.
    if (typeof Headers !== 'undefined') {
        const instance = new Headers({ 'X-A': 'b' });
        const hInit = { method: 'POST', headers: instance };
        const hNext = add(hInit, 'tok');
        assert.equal(instance.get('X-CSRF-Token'), null, 'caller Headers instance must not be mutated');
        assert.equal(hNext.headers.get('X-CSRF-Token'), 'tok');
        assert.equal(hNext.headers.get('X-A'), 'b');
    }

    // No headers at all.
    const bare = add({ method: 'POST' }, 'tok');
    assert.equal(bare.headers['X-CSRF-Token'], 'tok');

    console.log('PASS: test_addTokenHeader_preserves_and_does_not_mutate');
}

async function test_fetch_adds_header_to_unsafe_same_origin_api() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    await window.fetch('/api/thing', { method: 'POST', body: 'x' });
    assert.equal(fetchLog.length, 1);
    const { url, options } = fetchLog[0];
    assert.equal(url, '/api/thing');
    assert.equal(options.headers['X-CSRF-Token'], 'tok123');
    assert.equal(options.body, 'x', 'caller body must be preserved');
    console.log('PASS: test_fetch_adds_header_to_unsafe_same_origin_api');
}

async function test_fetch_skips_header_for_get_and_cross_origin() {
    const { window, fetchLog } = loadCSRF({ token: 'tok123' });
    await window.fetch('/api/thing'); // GET
    await window.fetch('http://evil.example/api/x', { method: 'POST' });
    await window.fetch('/some/page', { method: 'POST' });

    assert.equal(fetchLog.length, 3);
    assert.equal(fetchLog[0].options && fetchLog[0].options.headers, undefined, 'GET must not gain a header');
    assert.equal(fetchLog[1].options.headers && fetchLog[1].options.headers['X-CSRF-Token'], undefined, 'cross-origin must not gain a header');
    assert.equal(fetchLog[2].options.headers && fetchLog[2].options.headers['X-CSRF-Token'], undefined, 'non-/api/ path must not gain a header');
    console.log('PASS: test_fetch_skips_header_for_get_and_cross_origin');
}

async function test_no_token_in_meta_means_no_wrapping() {
    const { window, fetchLog } = loadCSRF({ token: '' });
    await window.fetch('/api/x', { method: 'POST', headers: { 'X-Custom': 'y' } });
    assert.equal(fetchLog.length, 1);
    assert.equal(fetchLog[0].options.headers['X-Custom'], 'y', 'caller header preserved');
    assert.equal(fetchLog[0].options.headers['X-CSRF-Token'], undefined);
    console.log('PASS: test_no_token_in_meta_means_no_wrapping');
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
    await test_fetch_adds_header_to_unsafe_same_origin_api();
    await test_fetch_skips_header_for_get_and_cross_origin();
    await test_no_token_in_meta_means_no_wrapping();
    await test_csrf_invalid_403_shows_prompt_without_retry();
    await test_non_csrf_error_passes_through_without_prompt();
    console.log('All CSRF frontend tests passed.');
}

main().catch((error) => {
    console.error('FAIL:', error);
    process.exitCode = 1;
});
