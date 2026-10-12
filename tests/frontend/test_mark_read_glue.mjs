// tests/frontend/test_mark_read_glue.mjs
//
// P2: the REAL maybeMarkChatRead()/markChatRead() from static/chat.js with a
// fake fetch: no POST on plain polls, one POST when a chat is opened with
// unread, one more only for a newer incoming message, the badge clears only
// after ok:true, failures back off for 30 s and never toast.
//
//   node tests/frontend/test_mark_read_glue.mjs

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const staticDir = path.join(__dirname, '..', '..', 'static');
const source = readFileSync(path.join(staticDir, 'chat.js'), 'utf8');
const viewsSource = readFileSync(path.join(staticDir, 'chat-views.js'), 'utf8');

function extractFunction(name) {
    const match = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(source);
    assert.ok(match, `function ${name} not found in static/chat.js`);
    const open = source.indexOf('{', source.indexOf(')', match.index));
    let depth = 0;
    for (let i = open; i < source.length; i++) {
        if (source[i] === '{') depth++;
        else if (source[i] === '}' && --depth === 0) return source.slice(match.index, i + 1);
    }
    throw new Error(`unbalanced braces in ${name}`);
}

function build({ respond }) {
    const calls = [];
    const warns = [];
    const toasts = [];
    const sandbox = {
        console: { log() {}, error() {}, warn: (...a) => warns.push(a.join(' ')) },
        Date, JSON, Number, Math,
        window: {},
        document: { hidden: false },
        currentChatId: '!a',
        chatListCache: [{ id: '!a', unread: 2 }],
        chatMarkReadState: {},
        loadChatListCalls: 0,
        loadChatList() { sandbox.loadChatListCalls += 1; },
        showToast: (m) => toasts.push(m),
        fetch: async (url, init) => {
            calls.push({ url, init });
            return respond(calls.length);
        },
        _now: 1_000_000,
    };
    sandbox.window = sandbox;
    // Date.now() is read through the sandbox clock so the 30 s back-off is testable.
    sandbox.Date = { now: () => sandbox._now };
    vm.createContext(sandbox);
    vm.runInContext(viewsSource, sandbox);
    for (const fn of ['maybeMarkChatRead', 'markChatRead']) vm.runInContext(extractFunction(fn), sandbox);
    return { sandbox, calls, warns, toasts };
}

const okResponse = (changed = true) => ({ ok: true, status: 200, json: async () => ({ ok: true, changed, unread: 0 }) });
const failResponse = () => ({ ok: false, status: 500, json: async () => ({ ok: false, error_code: 'storage_write_failed' }) });
const rx = (id) => ({ id, kind: 'rx' });
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

let passed = 0;
async function test(name, fn) { await fn(); passed += 1; }

await test('opening a chat with unread POSTs once, with the chat id, and clears the badge only after ok', async () => {
    const { sandbox, calls } = build({ respond: () => okResponse() });
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/api/messages/mark_read');
    assert.equal(calls[0].init.method, 'POST');
    assert.deepEqual(JSON.parse(calls[0].init.body), { chat_id: '!a' });
    assert.equal(sandbox.chatListCache[0].unread, 2, 'badge still there until the server confirmed');
    await settle();
    assert.equal(sandbox.chatListCache[0].unread, 0);
    assert.equal(sandbox.loadChatListCalls, 1);
});

await test('a minute of 2 s polls with nothing new sends no further POST', async () => {
    const { sandbox, calls } = build({ respond: () => okResponse() });
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    await settle();
    for (let i = 0; i < 30; i += 1) {
        sandbox._now += 2000;
        sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    }
    await settle();
    assert.equal(calls.length, 1);
});

await test('a new incoming message while open triggers exactly one more POST; own messages do not', async () => {
    const { sandbox, calls } = build({ respond: () => okResponse() });
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    await settle();
    sandbox.maybeMarkChatRead('!a', [rx('m1'), { id: 'mine', kind: 'me' }]);
    await settle();
    assert.equal(calls.length, 1, 'own message is not a reason');
    sandbox.maybeMarkChatRead('!a', [rx('m1'), rx('m2')]);
    sandbox.maybeMarkChatRead('!a', [rx('m1'), rx('m2')]);   // parallel poll while in flight
    await settle();
    assert.equal(calls.length, 2, 'one POST for m2 despite two overlapping loads');
});

await test('failure: warn only (no toast), 30 s back-off, then it retries', async () => {
    const { sandbox, calls, warns, toasts } = build({ respond: () => failResponse() });
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    await settle();
    assert.equal(calls.length, 1);
    assert.equal(warns.length, 1);
    assert.equal(toasts.length, 0);
    assert.equal(sandbox.chatListCache[0].unread, 2, 'badge stays: that is the true state');
    sandbox._now += 29_000;
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    await settle();
    assert.equal(calls.length, 1, 'inside the back-off window');
    sandbox._now += 2_000;
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    await settle();
    assert.equal(calls.length, 2, 'after 30 s');
});

await test('hidden tab or a chat that is no longer the open one: no POST', async () => {
    const { sandbox, calls } = build({ respond: () => okResponse() });
    sandbox.document.hidden = true;
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    sandbox.document.hidden = false;
    sandbox.currentChatId = '!other';
    sandbox.maybeMarkChatRead('!a', [rx('m1')]);
    await settle();
    assert.equal(calls.length, 0);
});

console.log(`test_mark_read_glue.mjs: ${passed} tests passed`);
