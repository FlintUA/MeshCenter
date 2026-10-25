// tests/frontend/test_login_countdown.mjs
//
// The login page counts the server's wait after wrong passwords down on the page (it used to print the number once
// and never change it). Runs the REAL inline script of templates/login.html in a vm sandbox with a fake DOM and clock.
// `node tests/frontend/test_login_countdown.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(__dirname, '..', '..', 'templates', 'login.html'), 'utf8').split('\r\n').join('\n');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1]);
const source = scripts.find((text) => text.includes('I18N.init'));
assert.ok(source, 'the page script is there');

function run({ retryAfter }) {
    let now = 1_000_000;
    const timers = [];
    const field = { focused: false, focus() { this.focused = true; } };
    const note = { dataset: retryAfter == null ? {} : { retryAfter: String(retryAfter) }, textContent: '', className: 'login-error' };
    const submit = { disabled: false };
    const win = {
        I18N: { init: () => Promise.resolve(), applyStaticDom() {}, t: (key, params) => (params ? key + ':' + params.seconds : key) },
        document: {
            getElementById: (id) => (id === 'loginError' ? (retryAfter == null ? null : note) : id === 'loginPassword' ? field : null),
            querySelector: (selector) => (selector === '.login-submit' ? submit : null),
        },
        Date: { now: () => now },
        setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearInterval: (id) => { if (timers[id - 1]) timers[id - 1].cleared = true; },
    };
    win.window = win;
    vm.createContext(win);
    vm.runInContext(source, win);
    return { note, submit, field, timers, advance: (seconds) => { now += seconds * 1000; for (const timer of timers) if (!timer.cleared) timer.fn(); } };
}

// the wait counts down, the button is locked, then the note says it can be tried again
{
    const page = run({ retryAfter: 24 });
    await new Promise((resolve) => setImmediate(resolve)); // I18N.init() resolves
    assert.equal(page.note.textContent, 'auth.login_throttled:24');
    assert.equal(page.submit.disabled, true, 'locked while the server would refuse');
    page.advance(1);
    assert.equal(page.note.textContent, 'auth.login_throttled:23', 'the number moves');
    page.advance(10);
    assert.equal(page.note.textContent, 'auth.login_throttled:13');
    page.advance(12.5);
    assert.equal(page.note.textContent, 'auth.login_throttled:1', 'a started second still counts');
    assert.equal(page.submit.disabled, true);
    page.advance(1);
    assert.equal(page.note.textContent, 'auth.login_throttle_over');
    assert.equal(page.note.className, 'login-info', 'no longer an error');
    assert.equal(page.submit.disabled, false, 'the button is back');
    assert.equal(page.field.focused, true, 'ready to type');
    assert.ok(page.timers.every((timer) => timer.cleared), 'the timer is stopped');
}

// a tab that was asleep: the deadline decides, not the number of ticks
{
    const page = run({ retryAfter: 30 });
    await new Promise((resolve) => setImmediate(resolve));
    page.advance(40);
    assert.equal(page.note.textContent, 'auth.login_throttle_over');
    assert.equal(page.submit.disabled, false);
}

// no wait (a plain wrong password, or the first visit): nothing is touched
{
    const page = run({ retryAfter: null });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(page.timers.length, 0);
    assert.equal(page.submit.disabled, false);
}

// every language has the new line
for (const loc of ['en', 'de', 'ru', 'uk']) {
    const cat = JSON.parse(readFileSync(path.join(__dirname, '..', '..', 'static', 'i18n', loc + '.json'), 'utf8'));
    assert.ok(cat.auth.login_throttle_over, loc + ' login_throttle_over');
}

console.log('test_login_countdown.mjs: ok');
