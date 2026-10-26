// tests/frontend/test_updates_checking_state.mjs
//
// UX1: pressing Update runs a preflight (a git fetch, several seconds) BEFORE the confirm dialog opens, and the
// card used to show nothing meanwhile. Now the status line and the button say "Checking..." at once, and both go
// back to normal when the preflight refuses, the dialog is cancelled or the request fails; after a confirmed
// update the restart poll takes over. Runs the REAL applyUpdate() from static/chat-updates-security.js.
// `node tests/frontend/test_updates_checking_state.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..', '..', 'static');
const source = readFileSync(path.join(root, 'chat-updates-security.js'), 'utf8').split('\r\n').join('\n');
const catalog = JSON.parse(readFileSync(path.join(root, 'i18n', 'en.json'), 'utf8'));
const lookup = (key) => key.split('.').reduce((node, part) => (node === undefined ? undefined : node[part]), catalog);
const I18N = {
    t(key, params) {
        const raw = lookup(key);
        assert.ok(typeof raw === 'string', `missing en.json key: ${key}`);
        return raw.replace(/\{(\w+)\}/g, (m, name) => (params && name in params ? String(params[name]) : m));
    },
};
function extractFunction(name) {
    const match = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(source);
    assert.ok(match, `${name} not found`);
    return source.slice(match.index, source.indexOf('\n}\n', match.index) + 3);
}

function makeEnv({ preflight, answer = true, applyResponse }) {
    const status = { textContent: 'Update available: 1.13.4' };
    const log = [];
    const toasts = [];
    const polls = [];
    let resolvePreflight;
    const gate = new Promise((resolve) => { resolvePreflight = resolve; });
    const sandbox = {
        console, Date, String, JSON, Promise,
        document: { getElementById: (id) => (id === 'updatesStatusText' ? status : null) },
        window: { I18N },
        _lastUpdatesStatus: { current_version: '1.13.3', latest_version: '1.13.4', last_checked_at: 1, check_ok: true, update_available: true },
        escapeHtml: (v) => String(v),
        showToast: (message, type) => toasts.push({ message, type }),
        appConfirm: async () => { log.push(`confirm(status="${status.textContent}")`); return answer; },
        updatesRefusalMessage: () => 'refused',
        renderUpdatesBlockedPanel() {},
        renderUpdatesStatus: (s) => { status.textContent = s.update_available ? `Update available: ${s.latest_version}` : 'Up to date'; },
        pollAfterUpdateRestart: (v) => polls.push(v),
        fetch: async (url) => {
            if (url === '/api/updates/preflight') { await gate; return { json: async () => preflight }; }
            return applyResponse || { ok: true, status: 200, json: async () => ({ ok: true }) };
        },
    };
    sandbox.window.window = sandbox.window;
    vm.createContext(sandbox);
    for (const name of ['_setUpdatesChecking', 'applyUpdate']) vm.runInContext(extractFunction(name), sandbox);
    return { sandbox, status, log, toasts, polls, release: () => resolvePreflight() };
}

// the preflight is slow: "Checking..." is on the screen at once, on the status line and the button
{
    const env = makeEnv({ preflight: { ok: true } });
    const button = { disabled: false, textContent: 'Update' };
    const run = env.sandbox.applyUpdate(button);
    assert.equal(env.status.textContent, catalog.system.updates_checking, 'status line says so while the preflight runs');
    assert.equal(button.textContent, catalog.system.updates_checking);
    assert.equal(button.disabled, true);
    env.release();
    await run;
    assert.deepEqual(env.log, ['confirm(status="Update available: 1.13.4")'], 'the dialog opens with the normal status, not a stale Checking...');
    assert.equal(button.textContent, 'Update', 'the button label is back');
    assert.deepEqual(env.polls, ['1.13.4'], 'a confirmed update hands over to the restart poll');
}

// the preflight refuses: normal again, the reason is a toast
{
    const env = makeEnv({ preflight: { ok: false, reason: 'ahead' } });
    const button = { disabled: false, textContent: 'Update' };
    const run = env.sandbox.applyUpdate(button);
    env.release();
    await run;
    assert.equal(env.status.textContent, 'Update available: 1.13.4');
    assert.equal(button.textContent, 'Update');
    assert.equal(button.disabled, false);
    assert.equal(env.toasts.length, 1);
}

// Cancel in the dialog: nothing is applied, the card is normal again
{
    const env = makeEnv({ preflight: { ok: true }, answer: false });
    const button = { disabled: false, textContent: 'Update' };
    const run = env.sandbox.applyUpdate(button);
    env.release();
    await run;
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, 'Update');
    assert.deepEqual(env.polls, []);
}

console.log('test_updates_checking_state.mjs: ok');
