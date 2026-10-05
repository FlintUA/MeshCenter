// tests/frontend/test_notifications_delegation.mjs
//
// H2-D / F1.2 (XSS inline-handler remediation, batch task): the Activity
// notifications card no longer builds onclick="fn('${id}')" attributes
// from template literals - it renders data-chat-action/data-id attributes
// and relies on one delegated document click listener, onChatActionClick(),
// to dispatch to markBackendNotificationRead()/deleteNotification(). This
// is the first area converted; see the H2-D PR description for the rest.
//
// Two things to prove, using the REAL functions from static/chat.js in
// node:vm (dependency-free, same shape as test_escaping.mjs):
//   1) renderNotificationsCard() never emits an onclick= attribute at all,
//      and a hostile notification id/title/body renders as literal text
//      (round-trips through HTML-attribute decoding unchanged).
//   2) onChatActionClick() dispatches to the right function with the
//      right argument for both the row click and the dismiss-button click
//      (which must NOT also trigger the row's mark-read action).

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat.js'), 'utf8');

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

function decodeAttr(value) {
    return value.replace(/&(#x[0-9a-f]+|#[0-9]+|amp|lt|gt|quot|apos);/gi, (m, e) => {
        const k = e.toLowerCase();
        if (k === 'amp') return '&';
        if (k === 'lt') return '<';
        if (k === 'gt') return '>';
        if (k === 'quot') return '"';
        if (k === 'apos') return "'";
        if (k.startsWith('#x')) return String.fromCodePoint(parseInt(k.slice(2), 16));
        return String.fromCodePoint(parseInt(k.slice(1), 10));
    });
}

const escapeHtmlSrc = extractFunction('escapeHtml');

// --- 1) renderNotificationsCard() output -----------------------------------

class FakeEl {
    constructor() { this.innerHTML = ''; this.style = {}; }
}

function buildRenderSandbox() {
    const list = new FakeEl();
    const elements = { notificationsList: list, notificationsBadge: new FakeEl(), notificationsClearBtn: new FakeEl(), notificationsEmpty: new FakeEl() };
    const sandbox = {
        console,
        document: { getElementById: (id) => elements[id] || null },
        window: { I18N: { t: (k) => `[[${k}]]` } },
        TimeFormatter: { formatTime: () => '12:00' },
        _notifCardExpanded: true, // avoid expandNotificationsCard()'s own fetch() path
        expandNotificationsCard: () => {},
    };
    vm.createContext(sandbox);
    vm.runInContext(`${escapeHtmlSrc}\n${extractFunction('renderNotificationsCard')}`, sandbox);
    return { sandbox, list };
}

const HOSTILE_ID = `n1" onclick="pwn()`;
const HOSTILE_TITLE = `</script><script>pwn()</script>`;
const HOSTILE_BODY = `x' onmouseover='pwn()`;

{
    const { sandbox, list } = buildRenderSandbox();
    sandbox.renderNotificationsCard(
        [{ id: HOSTILE_ID, title: HOSTILE_TITLE, body: HOSTILE_BODY, level: 'info', read: false, timestamp: Math.floor(Date.now() / 1000) }],
        1,
    );

    // A hostile id can legitimately contain the literal text "onclick=" as
    // escaped data (see list.innerHTML above) - that's not a live attribute,
    // just inert text inside data-id's quoted value. The real guard is the
    // source template itself: the two onclick= attributes this area used to
    // emit must be gone for good, not just happen to not fire on this input.
    assert.ok(!/onclick="markBackendNotificationRead/.test(source), 'markBackendNotificationRead must no longer be wired via onclick=');
    assert.ok(!/onclick="event\.stopPropagation\(\); deleteNotification/.test(source), 'deleteNotification must no longer be wired via onclick=');
    assert.ok(list.innerHTML.includes('data-chat-action="notif-mark-read"'), 'row must carry data-chat-action="notif-mark-read"');
    assert.ok(list.innerHTML.includes('data-chat-action="notif-dismiss"'), 'dismiss button must carry data-chat-action="notif-dismiss"');

    const idMatch = /data-id="([^"]*)"/.exec(list.innerHTML);
    assert.ok(idMatch, 'row must carry data-id');
    assert.equal(decodeAttr(idMatch[1]), HOSTILE_ID, 'data-id must round-trip to the exact original id');

    assert.ok(!list.innerHTML.includes('<script>pwn()'), 'hostile title must not render as a live <script> tag');
    // (The body/id escaping itself - that a quote can't break out of an
    // attribute - is test_escaping.mjs's job; this test only needs to
    // prove THIS render site actually calls escapeHtml() on n.body.)
    assert.ok(/escapeHtml\(n\.body\)/.test(source), 'n.body must be passed through escapeHtml()');
}

// --- 2) onChatActionClick() dispatch ----------------------------------------

function buildDispatchSandbox() {
    const calls = { markRead: [], dismiss: [] };
    const sandbox = {
        console,
        markBackendNotificationRead: (id, el) => calls.markRead.push([id, el]),
        deleteNotification: (id, el) => calls.dismiss.push([id, el]),
    };
    vm.createContext(sandbox);
    vm.runInContext(extractFunction('onChatActionClick'), sandbox);
    return { sandbox, calls };
}

// Minimal fake element supporting only what onChatActionClick() needs:
// closest() (self-or-ancestor by data-chat-action) and getAttribute().
class FakeTarget {
    constructor(attrs, parent) { this.attrs = attrs; this.parent = parent; }
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; }
    closest(selector) {
        // Only two selector shapes are ever used here.
        if (selector === '[data-chat-action]') {
            let node = this;
            while (node) {
                if (node.attrs['data-chat-action'] !== undefined) return node;
                node = node.parent;
            }
            return null;
        }
        if (selector === '.notifications-item') {
            let node = this;
            while (node) {
                if (node.attrs._class === 'notifications-item') return node;
                node = node.parent;
            }
            return null;
        }
        return null;
    }
}

{
    const { sandbox, calls } = buildDispatchSandbox();
    const row = new FakeTarget({ 'data-chat-action': 'notif-mark-read', 'data-id': HOSTILE_ID, _class: 'notifications-item' }, null);
    sandbox.onChatActionClick({ target: row, stopPropagation: () => { throw new Error('must not be called for a row click'); } });
    assert.deepEqual(calls.markRead, [[HOSTILE_ID, row]]);
    assert.equal(calls.dismiss.length, 0);
}

{
    const { sandbox, calls } = buildDispatchSandbox();
    const row = new FakeTarget({ _class: 'notifications-item', 'data-id': HOSTILE_ID }, null);
    const dismissBtn = new FakeTarget({ 'data-chat-action': 'notif-dismiss' }, row);
    let stopped = false;
    sandbox.onChatActionClick({ target: dismissBtn, stopPropagation: () => { stopped = true; } });
    assert.deepEqual(calls.dismiss, [[HOSTILE_ID, row]], 'dismiss must read the id from the closest .notifications-item, not its own attrs');
    assert.equal(calls.markRead.length, 0, 'dismissing must not also mark the row read');
    assert.ok(stopped, 'stopPropagation must be called for the dismiss branch');
}

console.log('test_notifications_delegation.mjs: ok');
