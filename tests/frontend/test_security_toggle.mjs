// tests/frontend/test_security_toggle.mjs
//
// Switching password protection off needs the current password. An empty "Current password" field used to
// be sent anyway: the server answered "incorrect" and counted a failed attempt, so a few clicks locked the user
// out ("Too many attempts"). Now an empty field sends nothing, puts the box back, says what is missing and
// focuses the field; a filled field is sent. Runs the REAL toggleSecurityEnabled() from
// static/chat-updates-security.js against the REAL en.json. `node tests/frontend/test_security_toggle.mjs`

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

function makeEnv(passwordValue, response) {
    const calls = [];
    const els = {
        securityCurrentPassword: { value: passwordValue, focused: false, focus() { this.focused = true; } },
        securityEnabled: { checked: false },
        securitySaveResult: { textContent: '', style: {} },
    };
    const context = {
        document: { getElementById: (id) => els[id] || null },
        window: { I18N },
        fetch: async (url, options) => { calls.push({ url, body: JSON.parse(options.body) }); return { json: async () => response }; },
        renderSecurityStatus() {},
        console,
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(['renderSecurityResult', '_securityCurrentPasswordErrorMessage', 'toggleSecurityEnabled'].map(extractFunction).join('\n'), context);
    return { context, els, calls };
}

// unchecked with an empty field: nothing is sent, the box goes back, the field is asked for
{
    const { context, els, calls } = makeEnv('', { ok: true });
    await context.toggleSecurityEnabled(false);
    assert.equal(calls.length, 0, 'no request: no failed attempt is counted');
    assert.equal(els.securityEnabled.checked, true, 'the box is put back');
    assert.equal(els.securityCurrentPassword.focused, true);
    assert.equal(els.securitySaveResult.textContent, catalog.system.security_current_password_required);
}

// unchecked with a password: it is sent
{
    const { context, els, calls } = makeEnv('secret-password-1', { ok: true });
    await context.toggleSecurityEnabled(false);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].body, { enabled: false, current_password: 'secret-password-1' });
    assert.equal(els.securityCurrentPassword.value, '', 'the field is cleared after success');
}

// checking the box never needs the password
{
    const { context, calls } = makeEnv('', { ok: true });
    await context.toggleSecurityEnabled(true);
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].body, { enabled: true });
}

// a wrong password puts the box back; a lock-out names the seconds
{
    const wrong = makeEnv('nope', { ok: false, error_code: 'current_password_invalid' });
    await wrong.context.toggleSecurityEnabled(false);
    assert.equal(wrong.els.securityEnabled.checked, true);
    assert.equal(wrong.els.securitySaveResult.textContent, catalog.system.security_current_password_invalid);
    const locked = makeEnv('nope', { ok: false, error_code: 'login_throttled', retry_after: 42 });
    await locked.context.toggleSecurityEnabled(false);
    assert.match(locked.els.securitySaveResult.textContent, /42/);
}

console.log('test_security_toggle.mjs: ok');
