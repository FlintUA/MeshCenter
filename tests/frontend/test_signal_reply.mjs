// tests/frontend/test_signal_reply.mjs
//
// SIG1: (1) "Reply with signal" - the text builder (language-neutral, real newlines, <= 120 bytes), the
// menu rules (hidden / disabled / visible) and the send path (a native reply into the SAME chat, at once);
// (2) a literal "\n" in a received text is shown as a line break at display time only - after escaping,
// never building HTML from the text. Runs the REAL source (static/chat-views.js, functions of
// static/chat.js) in a vm sandbox. `node tests/frontend/test_signal_reply.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');

// ---------------------------------------------------------------- load MCViews
const window = {};
vm.createContext(window);
window.window = window;
vm.runInContext(read('chat-views.js'), window);
const V = window.MCViews;
assert.ok(V && V.buildSignalReplyText, 'MCViews.buildSignalReplyText is exported');

const plain = (value) => JSON.parse(JSON.stringify(value));
const at = (h, m, s) => Math.floor(new Date(2026, 9, 21, h, m, s).getTime() / 1000); // local time, TZ independent

// ---------------------------------------------------------------- the text builder
{
    const msg = { kind: 'rx', packet_id: 1, ts: at(16, 28, 6), rx: { snr: -5.5, rssi: -87, hop_start: 3, hop_limit: 0 } };
    assert.equal(V.buildSignalReplyText(msg), 'Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm\nHops 3', 'the exact format');

    // hops 0 = direct
    assert.equal(
        V.buildSignalReplyText({ ...msg, rx: { snr: 6.25, rssi: -60, hop_start: 3, hop_limit: 3 } }),
        'Rx 16:28:06\nSNR 6.25 dB | RSSI -60 dBm\nHops 0 (direct)');

    // hops unknown -> the line is omitted (no hop fields, one missing, hop_start 0 = unknown on old firmware)
    for (const rx of [{ snr: -5.5, rssi: -87 }, { snr: -5.5, rssi: -87, hop_start: 3 }, { snr: -5.5, rssi: -87, hop_limit: 3 },
        { snr: -5.5, rssi: -87, hop_start: 0, hop_limit: 3 }, { snr: -5.5, rssi: -87, hop_start: 2, hop_limit: 3 }]) {
        assert.equal(V.buildSignalReplyText({ ...msg, rx }), 'Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm', JSON.stringify(rx));
    }

    // a missing metric is omitted
    assert.equal(V.buildSignalReplyText({ ...msg, rx: { snr: -5.5, hop_start: 3, hop_limit: 1 } }), 'Rx 16:28:06\nSNR -5.5 dB\nHops 2');
    assert.equal(V.buildSignalReplyText({ ...msg, rx: { rssi: -101 } }), 'Rx 16:28:06\nRSSI -101 dBm');

    // neither SNR nor RSSI -> no text at all (the item is hidden)
    for (const rx of [undefined, null, {}, { hop_start: 3, hop_limit: 1 }, { snr: null, rssi: null }, { snr: 'x', rssi: NaN }]) {
        assert.equal(V.buildSignalReplyText({ ...msg, rx }), null, JSON.stringify(rx));
    }
    assert.equal(V.buildSignalReplyText(null), null);
    assert.equal(V.buildSignalReplyText({ kind: 'rx' }), null);

    // real newline characters, never a literal backslash-n
    const text = V.buildSignalReplyText(msg);
    assert.equal(text.split('\n').length, 3);
    assert.ok(!text.includes('\\'));

    // time: the stored string when there is no epoch; omitted when neither is usable
    assert.equal(V.buildSignalReplyText({ time: '09:05:07', rx: { snr: 1, rssi: -50 } }), 'Rx 09:05:07\nSNR 1 dB | RSSI -50 dBm');
    assert.equal(V.buildSignalReplyText({ time: 'yesterday', rx: { snr: 1, rssi: -50 } }), 'SNR 1 dB | RSSI -50 dBm');
    assert.equal(V.buildSignalReplyText({ ts: at(0, 0, 0), rx: { snr: 1 } }).split('\n')[0], 'Rx 00:00:00');
}

// ---------------------------------------------------------------- the byte budget
{
    const worst = {
        kind: 'rx', packet_id: 1, ts: at(23, 59, 59),
        rx: { snr: -12.25, rssi: -118, hop_start: 7, hop_limit: 0 },
        // names never go into the text: an emoji name must not change a thing
        sender: '\u{1F340}\u{1F6F4}\u{1F680} MMSC Boden World \u{1F30D}', node_name: '\u{1F340}'.repeat(30),
    };
    const text = V.buildSignalReplyText(worst);
    assert.equal(text, 'Rx 23:59:59\nSNR -12.25 dB | RSSI -118 dBm\nHops 7');
    assert.ok(V.utf8Length(text) <= 120, `worst case is ${V.utf8Length(text)} bytes`);
    assert.ok(V.utf8Length(text) < 60, 'plenty of margin below the 120 byte target');
    assert.ok(!text.includes('MMSC') && !/[^\x00-\x7f]/.test(text), 'ASCII only, no names');
    assert.equal(V.utf8Length('\u00e4'), 2);
}

// ---------------------------------------------------------------- menu visibility rules
{
    const base = { kind: 'rx', packet_id: 42, ts: at(16, 28, 6), rx: { snr: -5.5, rssi: -87 } };
    const state = (m, o) => plain(V.signalReplyMenuState(m, o));
    assert.equal(state(base, {}).visible, true);
    assert.equal(state(base, {}).disabled, false);
    assert.equal(state(base, {}).text, 'Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm');
    // our own messages, system lines, sent records
    assert.equal(state(base, { isOwn: true }).visible, false, 'own message');
    assert.equal(state({ ...base, kind: 'me' }, {}).visible, false);
    assert.equal(state({ ...base, kind: 'system' }, {}).visible, false);
    assert.equal(state({ ...base, kind: 'tx' }, {}).visible, false);
    // no metrics / no packet id (a native reply needs the packet to link to)
    assert.equal(state({ ...base, rx: undefined }, {}).visible, false, 'no radio metrics');
    assert.equal(state({ ...base, rx: { hop_start: 3, hop_limit: 1 } }, {}).visible, false, 'neither SNR nor RSSI');
    assert.equal(state({ ...base, packet_id: undefined }, {}).visible, false, 'no packet id');
    assert.equal(state({ ...base, packet_id: null }, {}).visible, false);
    assert.equal(state(null, {}).visible, false);
    // released radio: shown but disabled, with the composer's reason
    const released = state(base, { releasedReason: 'Radio released - press Reconnect' });
    assert.equal(released.visible, true);
    assert.equal(released.disabled, true);
    assert.equal(released.reason, 'Radio released - press Reconnect');
}

// ---------------------------------------------------------------- display: literal \n -> line break
{
    const E = (t) => V.expandEscapedNewlines(t);
    assert.equal(E('a\\nb'), 'a\nb', 'literal backslash-n');
    assert.equal(E('a\\r\\nb'), 'a\nb', 'literal backslash-r backslash-n');
    assert.equal(E('a\nb'), 'a\nb', 'a real newline stays');
    assert.equal(E('a\r\nb'), 'a\nb', 'a real CR LF is normalised');
    assert.equal(E('a\\nb\\nc\nd'), 'a\nb\nc\nd', 'mixed');
    assert.equal(E('C:\\temp\\x and \\t and \\ alone'), 'C:\\temp\\x and \\t and \\ alone', 'a backslash not followed by n stays');
    assert.equal(E('no escapes at all'), 'no escapes at all');
    assert.equal(E(''), '');
    assert.equal(E(null), '');
    assert.equal(E(undefined), '');
    assert.equal(E(42), '42');
    // the real example from the field (FMG Kalchreuth-Bayern)
    assert.equal(E('Station Kalchreuth\\nEmpfang 16:26:52\\nSNR -6.5 dB'), 'Station Kalchreuth\nEmpfang 16:26:52\nSNR -6.5 dB');

    // one-line previews: line breaks become " \u00b7 "
    const P = (t) => V.previewText(t);
    assert.equal(P('Station Kalchreuth\\nEmpfang 16:26:52\\nSNR -6.5 dB'), 'Station Kalchreuth \u00b7 Empfang 16:26:52 \u00b7 SNR -6.5 dB');
    assert.equal(P('Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm\nHops 3'), 'Rx 16:28:06 \u00b7 SNR -5.5 dB | RSSI -87 dBm \u00b7 Hops 3');
    assert.equal(P('one line'), 'one line');
    assert.equal(P('a\\n\\nb\n\n'), 'a \u00b7 b', 'empty lines do not produce empty segments');
    assert.equal(P(''), '');
}

// ---------------------------------------------------------------- safety: conversion happens BEFORE escaping
const escapeHtml = (value) => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
{
    const hostile = '<b>bold</b>\\n</div><img src=x onerror=alert(1)>\\r\\n<script>alert(2)</script>';
    const html = escapeHtml(V.expandEscapedNewlines(hostile));
    assert.ok(!/<b>|<\/div>|<img|<script/i.test(html), 'no markup survives: ' + html);
    assert.equal(html.split('\n').length, 3, 'the two literal \\n became real line breaks');
    assert.match(html, /^&lt;b&gt;bold&lt;\/b&gt;\n&lt;\/div&gt;/);
    assert.ok(!html.includes('\\n'));
    // the preview is escaped by its caller as well
    assert.ok(!/<b>/.test(escapeHtml(V.previewText(hostile))));
}

// ---------------------------------------------------------------- chat.js: the real functions
const chatSource = read('chat.js');
function extractFunction(name) {
    const match = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(chatSource);
    assert.ok(match, `${name} not found`);
    const end = chatSource.indexOf('\n}\n', match.index);
    return chatSource.slice(match.index, end + 3);
}

function makeChatEnv({ own = false, released = null, chat = { id: 'channel:1', type: 'channel', name: 'Flint-pvt [1]' } } = {}) {
    const calls = { submitted: [], menuClosed: 0 };
    const item = { hidden: true, disabled: false, attrs: {}, title: undefined,
        setAttribute(k, v) { this.attrs[k] = v; }, removeAttribute(k) { if (k === 'title') this.title = undefined; } };
    const sandbox = {
        console, String, Number, Math, Boolean, JSON,
        window: null,
        document: { getElementById: (id) => (id === 'messageActionSignalReply' ? item : null) },
        MCViews: V,
        radioReleasedInfo: released,
        messageActionTarget: null,
        currentChatId: chat.id, currentChatType: chat.type, currentChatName: chat.name,
        messageBelongsToActiveRadio: () => own,
        closeMessageActionsMenu: () => { calls.menuClosed += 1; },
        buildReplyPayload: (m) => ({ id: m.id, packet_id: m.packet_id, text: m.text, chat_id: m.chat_id }),
        submitOutgoingMessage: async (...args) => { calls.submitted.push(args); },
    };
    sandbox.window = sandbox;
    sandbox.I18N = { t: (key) => ({ 'chat.radio_released_placeholder': 'Radio released - press Reconnect' }[key] || key) };
    vm.createContext(sandbox);
    for (const name of ['signalReplyState', 'updateSignalReplyMenuItem', 'replyWithSignal']) vm.runInContext(extractFunction(name), sandbox);
    return { sandbox, calls, item };
}

const received = { id: 'm1', kind: 'rx', packet_id: 777, text: 'original', ts: at(16, 28, 6), chat_id: 'channel:1', rx: { snr: -5.5, rssi: -87, hop_start: 3, hop_limit: 0 } };

// send path: native reply (the original packet id) into the SAME chat, immediately
{
    const env = makeChatEnv();
    env.sandbox.messageActionTarget = received;
    await env.sandbox.replyWithSignal();
    assert.equal(env.calls.menuClosed, 1);
    assert.equal(env.calls.submitted.length, 1, 'sent at once: one submission, no composer step');
    const [chatId, chatType, chatName, text, replyTo] = env.calls.submitted[0];
    assert.deepEqual([chatId, chatType, chatName], ['channel:1', 'channel', 'Flint-pvt [1]'], 'same chat');
    assert.equal(text, 'Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm\nHops 3');
    assert.equal(replyTo.packet_id, 777, 'reply_id = the original packet id');
    assert.equal(replyTo.id, 'm1');
}

// a DM goes back to the same DM
{
    const env = makeChatEnv({ chat: { id: '!b0f14d2a', type: 'dm', name: 'Flint_Echo' } });
    env.sandbox.messageActionTarget = { ...received, chat_id: '!b0f14d2a' };
    await env.sandbox.replyWithSignal();
    assert.deepEqual(env.calls.submitted[0].slice(0, 3), ['!b0f14d2a', 'dm', 'Flint_Echo']);
}

// nothing is sent when the item would not be offered: own message, no metrics, released radio, no target
for (const [label, setup] of [
    ['own message', { own: true }],
    ['released radio', { released: { kind: 'tcp' } }],
]) {
    const env = makeChatEnv(setup);
    env.sandbox.messageActionTarget = received;
    await env.sandbox.replyWithSignal();
    assert.equal(env.calls.submitted.length, 0, label);
}
{
    const env = makeChatEnv();
    env.sandbox.messageActionTarget = { ...received, rx: {} };
    await env.sandbox.replyWithSignal();
    assert.equal(env.calls.submitted.length, 0, 'no metrics');
    env.sandbox.messageActionTarget = null;
    await env.sandbox.replyWithSignal();
    assert.equal(env.calls.submitted.length, 0, 'no target');
}

// the menu item: hidden / visible / disabled with the composer's reason
{
    const env = makeChatEnv();
    env.sandbox.updateSignalReplyMenuItem(received);
    assert.equal(env.item.hidden, false);
    assert.equal(env.item.disabled, false);
    assert.equal(env.item.attrs['aria-disabled'], 'false');

    env.sandbox.updateSignalReplyMenuItem({ ...received, rx: undefined });
    assert.equal(env.item.hidden, true, 'no metrics');

    const own = makeChatEnv({ own: true });
    own.sandbox.updateSignalReplyMenuItem(received);
    assert.equal(own.item.hidden, true, 'own message');

    const rel = makeChatEnv({ released: { kind: 'serial' } });
    rel.sandbox.updateSignalReplyMenuItem(received);
    assert.equal(rel.item.hidden, false);
    assert.equal(rel.item.disabled, true);
    assert.equal(rel.item.attrs['aria-disabled'], 'true');
    assert.equal(rel.item.title, 'Radio released - press Reconnect', 'same reason as the composer');
}

// the reply quote is escaped AFTER the line breaks are expanded
{
    const sandbox = { console, String, window: null, MCViews: V, escapeHtml };
    sandbox.window = sandbox;
    sandbox.I18N = { t: (k) => k };
    vm.createContext(sandbox);
    vm.runInContext(extractFunction('buildReplyBlockHtml'), sandbox);
    const html = sandbox.buildReplyBlockHtml({ reply_to: { id: 'x', sender: '<i>S</i>', text: '<b>hi</b>\\nsecond line' } });
    assert.ok(!/<b>|<i>/.test(html), 'no markup from message text');
    assert.match(html, /&lt;b&gt;hi&lt;\/b&gt;\nsecond line/);
}

console.log('OK - signal reply (text builder, byte budget, menu rules, send path) and literal \\n display');
