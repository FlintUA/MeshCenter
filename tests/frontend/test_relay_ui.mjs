// tests/frontend/test_relay_ui.mjs
//
// SIG4: the last relay of a packet in the UI. (1) "Reply with signal" gets a "Via <short name>" line ONLY for a
// resolved relay (never for direct, ambiguous, unknown) and only while the whole text stays small; (2) Message
// Info describes the relay (resolved / direct / possibly / unknown / none); (3) the "Trace route to sender" menu
// item: hidden for own and system messages, disabled with a reason on TCP, while released, busy or inside the
// 60 s block. Runs the REAL static/chat-views.js in a vm sandbox. `node tests/frontend/test_relay_ui.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat-views.js'), 'utf8').split('\r\n').join('\n');
const window = {};
vm.createContext(window);
window.window = window;
vm.runInContext(source, window);
const V = window.MCViews;
const plain = (value) => JSON.parse(JSON.stringify(value));

const base = { snr: -5.5, rssi: -87, hop_start: 4, hop_limit: 1, relay_node: 0x84 };
const msg = (relay, extra = {}) => ({ kind: 'rx', packet_id: 7, node_id: '!aabbcc11', ts: 0, time: '16:28:06', rx: { ...base, relay, ...extra } });

// ---- the Via line
{
    const resolved = { status: 'resolved', node_id: '!aaaaaa84', short_name: 'FMG', basis: 'direct24h' };
    assert.equal(V.buildSignalReplyText(msg(resolved)), 'Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm\nHops 3\nVia FMG');
    for (const status of ['direct', 'ambiguous', 'unknown']) {
        const text = V.buildSignalReplyText(msg({ status, short_name: 'X', candidates: [{ node_id: '!aaaaaa84', short_name: 'A' }] }));
        assert.ok(!text.includes('Via'), `${status}: no Via line`);
    }
    assert.ok(!V.buildSignalReplyText(msg(undefined)).includes('Via'), 'no relay at all: no Via line');
    // non-ASCII (emoji) names are stripped, an empty result leaves the line out
    assert.equal(V.buildSignalReplyText(msg({ status: 'resolved', short_name: 'FMG🙂' })).split('\n').pop(), 'Via FMG');
    assert.ok(!V.buildSignalReplyText(msg({ status: 'resolved', short_name: '🙂🙂' })).includes('Via'));
    // a long name is cut so the text stays well inside the radio's limit
    const long = V.buildSignalReplyText(msg({ status: 'resolved', short_name: 'ABCDEFGHIJKLMNOP' }));
    assert.equal(long.split('\n').pop(), 'Via ABCDEFGH');
    assert.ok(new TextEncoder().encode(long).length <= 120);
}

// ---- Message Info
{
    assert.deepEqual(plain(V.relayInfo({})), { kind: 'none', hex: '', names: [], count: 0 });
    assert.equal(V.relayInfo({ relay_node: 0 }).kind, 'none');
    assert.deepEqual(plain(V.relayInfo({ relay_node: 5, relay: { status: 'unknown' } })), { kind: 'unknown', hex: '0x05', names: [], count: 0 });
    assert.deepEqual(plain(V.relayInfo({ relay_node: 0x84, relay: { status: 'resolved', short_name: 'FMG' } })), { kind: 'resolved', hex: '0x84', names: ['FMG'], count: 1 });
    assert.equal(V.relayInfo({ relay_node: 0x84, relay: { status: 'direct', short_name: 'SEND' } }).kind, 'direct');
    const amb = V.relayInfo({ relay_node: 0x84, relay: { status: 'ambiguous', count: 6, candidates: [{ short_name: 'A' }, { short_name: 'B' }, { node_id: '!aaaaaa84' }] } });
    assert.deepEqual(plain(amb), { kind: 'ambiguous', hex: '0x84', names: ['A', 'B', '!aaaaaa84'], count: 6 });
    assert.equal(V.relayInfo({ relay_node: 0x84 }).kind, 'unknown', 'a byte whose resolution was not stored is shown as unknown');
}

// ---- Trace route menu item
{
    const rx = msg({ status: 'unknown' });
    const state = (options, message = rx) => plain(V.tracerouteMenuState(message, options));
    assert.deepEqual(state({}), { visible: true, disabled: false, reasonKey: '', retryS: 0 });
    assert.equal(state({ isOwn: true }).visible, false, 'own message: hidden');
    assert.equal(V.tracerouteMenuState({ ...rx, kind: 'system' }, {}).visible, false, 'system message: hidden');
    assert.equal(V.tracerouteMenuState({ ...rx, kind: 'me' }, {}).visible, false);
    assert.equal(V.tracerouteMenuState({ ...rx, node_id: '' }, {}).visible, false, 'no sender node: hidden');
    assert.equal(V.tracerouteMenuState({ ...rx, node_id: '^all' }, {}).visible, false);
    assert.equal(V.tracerouteMenuState(null, {}).visible, false);
    assert.deepEqual(state({ transportType: 'tcp' }), { visible: true, disabled: true, reasonKey: 'tcp', retryS: 0 });
    assert.equal(state({ transportType: 'TCP' }).reasonKey, 'tcp');
    assert.equal(state({ transportType: 'serial' }).disabled, false);
    assert.equal(state({ released: true }).reasonKey, 'released');
    assert.equal(state({ busy: true }).reasonKey, 'busy');
    assert.deepEqual(state({ cooldownLeftS: 41.2 }), { visible: true, disabled: true, reasonKey: 'cooldown', retryS: 42 });
    assert.equal(state({ cooldownLeftS: 0 }).disabled, false, 'the block is over');
    assert.equal(state({ transportType: 'tcp', cooldownLeftS: 30 }).reasonKey, 'tcp', 'the transport is the more basic reason');
}

console.log('test_relay_ui.mjs: ok');
