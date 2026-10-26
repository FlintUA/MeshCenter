// tests/frontend/test_mobile_chat_pages.mjs
//
// MOB2-C: on a phone Chats is two pages. static/mobile-layout.js keeps body[data-mobile-chat] = list |
// conversation: opening a chat (openChat) shows the conversation and the Back button, showChatList() or a tap
// on Back returns to the list. On a wide viewport nothing is set and the Back button stays hidden.
// `node tests/frontend/test_mobile_chat_pages.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'mobile-layout.js'), 'utf8');

function makeEnv(mobile) {
    const attrs = {};
    const listeners = {};
    const calls = [];
    const back = { id: 'chatBackBtn', hidden: true, closest: () => back, classList: { contains: () => false, toggle() {} } };
    const body = { setAttribute: (k, v) => { attrs[k] = v; }, removeAttribute: (k) => { delete attrs[k]; } };
    const document = {
        readyState: 'complete', body,
        getElementById: (id) => (id === 'chatBackBtn' ? back : null),
        querySelector: () => null,
        addEventListener: (name, fn) => { listeners[name] = fn; },
    };
    const window = {
        matchMedia: () => ({ matches: mobile, addEventListener() {} }),
        document, addEventListener() {},
        openChat: () => { calls.push('openChat'); },
        showChatList: () => { calls.push('showChatList'); },
    };
    const ctx = { window, document, setTimeout: (fn) => fn(), MutationObserver: undefined };
    vm.createContext(ctx);
    vm.runInContext(source, ctx);
    return { window: ctx.window, api: ctx.window.MCMobileLayout, attrs, back, calls, click: (target) => listeners.click({ target, preventDefault() {}, stopImmediatePropagation() {} }) };
}

// a phone starts on the list page, the Back button hidden
{
    const env = makeEnv(true);
    assert.equal(env.attrs['data-mobile-chat'], 'list');
    assert.equal(env.back.hidden, true);
    assert.equal(env.api.getChatPage(), 'list');
}

// opening a chat: the conversation page with the Back button; the original function still runs
{
    const env = makeEnv(true);
    env.window.openChat('!756f9960', 'Flint TAP2', 'dm');
    assert.deepEqual(env.calls, ['openChat']);
    assert.equal(env.attrs['data-mobile-chat'], 'conversation');
    assert.equal(env.back.hidden, false);
}

// showing the list again (the Back button, a deleted chat): back to the list page
{
    const env = makeEnv(true);
    env.window.openChat('channel', 'LongFast', 'channel');
    env.window.showChatList();
    assert.equal(env.attrs['data-mobile-chat'], 'list');
    assert.equal(env.back.hidden, true);
    env.window.openChat('channel', 'LongFast', 'channel');
    env.calls.length = 0;
    env.click(env.back); // a tap on Back calls the (wrapped) showChatList
    assert.deepEqual(env.calls, ['showChatList']);
    assert.equal(env.attrs['data-mobile-chat'], 'list');
}

// wide viewport: no attribute, Back stays hidden, the functions still run
{
    const env = makeEnv(false);
    env.window.openChat('channel', 'LongFast', 'channel');
    assert.equal('data-mobile-chat' in env.attrs, false);
    assert.equal(env.back.hidden, true);
    assert.deepEqual(env.calls, ['openChat']);
}

// the wrappers are installed once
{
    const env = makeEnv(true);
    env.api._wrapShowChatList();
    env.api._wrapOpenChat();
    env.window.openChat('x', 'x', 'dm');
    assert.deepEqual(env.calls, ['openChat'], 'not wrapped twice');
}

console.log('test_mobile_chat_pages.mjs: ok');
