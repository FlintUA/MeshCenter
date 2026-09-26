// tests/frontend/test_non_serial_listener_ui.mjs
//
// TCP/Bluetooth have no Core `--listen` subprocess, so radio_health's
// `listener_running` is always false for them - a known limitation, not a
// fault. Live on pixel-111 (TCP CONNECTED, identity MATCH) the header showed a
// red "Offline", the System card "Listener: Stopped", and the device label
// "Listener stopped". This runs the REAL functions from static/chat.js (pulled
// out by name, chat.js as a whole needs a full DOM to load) in a vm sandbox
// against stub DOM/I18N. Dependency-free: `node tests/frontend/test_non_serial_listener_ui.mjs`.

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

const STRINGS = {
    'settings.radio_status_connected': 'Connected',
    'settings.radio_status_reconnecting': 'Reconnecting',
    'settings.radio_status_released': 'Released',
    'settings.radio_status_releasing': 'Releasing',
    'node_manager.connection_error': 'Error',
    'node_manager.connection_disconnected': 'Disconnected',
    'node_manager.listener_stopped': 'Listener stopped',
    'node_manager.running': 'Running',
    'node_manager.stopped': 'Stopped',
    'system.reception_not_supported': 'Not received (send only)',
    'system.restart_listener_not_applicable': 'Not applicable for TCP/Bluetooth connections',
    'system.restart_listener_button_label': 'Restart Listener',
    'settings.meshtastic_tcp_receive_note': 'TCP-NOTE',
    'settings.meshtastic_ble_receive_warning': 'BLE-NOTE',
};

class El {
    constructor(id) {
        this.id = id;
        this.textContent = '';
        this.title = '';
        this.disabled = false;
        this.style = { display: '' };
        this._attrs = {};
        this._classes = new Set();
        this.classList = {
            add: (...c) => c.forEach(x => this._classes.add(x)),
            remove: (...c) => c.forEach(x => this._classes.delete(x)),
        };
        this.parent = null;
        this.innerHTML = '';
    }
    setAttribute(k, v) { this._attrs[k] = v; }
    querySelector() { return this._label || (this._label = new El('label')); }
    closest() { return this.parent; }
}

function build(healthData) {
    const els = new Map();
    const get = (id) => {
        if (!els.has(id)) els.set(id, new El(id));
        return els.get(id);
    };
    const listenerRow = new El('listenerRow');
    get('radioHealthListener').parent = listenerRow;

    const sandbox = {
        console,
        document: { getElementById: get },
        window: {
            I18N: {
                t: (k) => (k in STRINGS ? STRINGS[k] : `[[${k}]]`),
                tOrFallback: (k, p, fb) => (k in STRINGS ? STRINGS[k] : fb),
            },
        },
        fetch: async (url) => {
            if (url === '/api/radio_health') return { ok: true, json: async () => healthData };
            return { ok: true, json: async () => ({ events: [] }) };
        },
        escapeHtml: (v) => String(v ?? ''),
        TimeFormatter: { formatDateTime: () => '' },
        _lastSystemLogEvents: [],
    };
    vm.createContext(sandbox);
    for (const fn of ['isNonSerialTransport', 'deviceConnectionLabel', 'updateHeaderNodeStatus', 'loadRadioHealth']) {
        vm.runInContext(extractFunction(fn), sandbox);
    }
    sandbox._els = els;
    sandbox._listenerRow = listenerRow;
    return sandbox;
}

function header(sb) {
    const el = sb.document.getElementById('headerStatusText');
    return el;
}

function runHeader(data) {
    const sb = build(data);
    const h = header(sb);
    h.querySelector = () => h._label || (h._label = new El('label'));
    sb.updateHeaderNodeStatus(data, true);
    return { sb, h, label: h._label ? h._label.textContent : '' };
}

const TCP_CONNECTED = {
    status: 'CONNECTED', level: 'OK', listener_running: false, transport: 'tcp',
    packet_age: null, status_reason: 'Connected via tcp (192.168.2.34:4403)',
};

// --- header ------------------------------------------------------------

{
    const { h, label } = runHeader(TCP_CONNECTED);
    assert.equal(label, 'Online', 'a connected TCP radio must not read "Offline"');
    assert.ok(h._classes.has('status-ok') && !h._classes.has('status-error'));
    assert.ok(!h.title.includes('Listener'), 'no listener wording in the tooltip for TCP');
}

{
    const { label, h } = runHeader({ ...TCP_CONNECTED, transport: 'bluetooth' });
    assert.equal(label, 'Online');
    assert.ok(!h.title.includes('Listener'));
}

{
    // A genuinely broken TCP link is still surfaced (via transport status/level).
    const { label, h } = runHeader({ ...TCP_CONNECTED, status: 'DISCONNECTED', level: 'ERROR' });
    assert.equal(label, 'Error');
    assert.ok(h._classes.has('status-error'));
}

{
    // Serial is unchanged: listener down => Offline, and the listener is shown.
    const { label, h } = runHeader({ status: 'LISTENER_DOWN', level: 'ERROR', listener_running: false, transport: 'serial' });
    assert.equal(label, 'Offline');
    assert.ok(h.title.includes('Listener: stopped'));
}

{
    // Older payload without `transport` behaves as serial (no regression).
    const { label } = runHeader({ status: 'OK', level: 'OK', listener_running: false });
    assert.equal(label, 'Offline');
}

// --- device connection label ---------------------------------------------

{
    const sb = build({});
    assert.equal(sb.deviceConnectionLabel('connected', false, 'tcp'), 'Connected');
    assert.equal(sb.deviceConnectionLabel('connected', false, 'bluetooth'), 'Connected');
    assert.equal(sb.deviceConnectionLabel('connecting', false, 'tcp'), 'Reconnecting');
    assert.equal(sb.deviceConnectionLabel('disconnected', false, 'tcp'), 'Disconnected');
    assert.equal(sb.deviceConnectionLabel('error', false, 'tcp'), 'Error');
    for (const mode of ['connected', 'connecting', 'disconnected', 'error']) {
        assert.ok(!/listener/i.test(sb.deviceConnectionLabel(mode, false, 'tcp')), `${mode}: no "listener" wording for tcp`);
    }
    // Serial unchanged.
    assert.equal(sb.deviceConnectionLabel('connected', true, 'serial'), 'Connected');
    assert.equal(sb.deviceConnectionLabel('connected', false, 'serial'), 'Listener stopped');
    assert.equal(sb.deviceConnectionLabel('connected', false), 'Listener stopped');
    assert.equal(sb.deviceConnectionLabel('released', false, 'serial'), 'Released');
}

// --- System card (loadRadioHealth) ----------------------------------------

async function runCard(data) {
    const sb = build(data);
    const h = header(sb);
    h.querySelector = () => h._label || (h._label = new El('label'));
    await sb.loadRadioHealth();
    return sb;
}

{
    const sb = await runCard(TCP_CONNECTED);
    const get = (id) => sb.document.getElementById(id);
    assert.equal(sb._listenerRow.style.display, 'none', 'the Listener row is hidden for TCP');
    assert.notEqual(get('radioHealthListener').textContent, 'Stopped');
    assert.ok(!/Stopped/.test(get('radioHealthListener').textContent));
    assert.equal(get('radioHealthReceiveRow').style.display, '');
    assert.equal(get('radioHealthReceive').textContent, 'Not received (send only)');
    assert.equal(get('radioHealthReceive').title, 'TCP-NOTE');
    assert.equal(get('restartListenerBtn').disabled, true, 'Restart Listener is disabled, not hidden');
    assert.equal(get('restartListenerBtn').title, 'Not applicable for TCP/Bluetooth connections');
}

{
    const sb = await runCard({ ...TCP_CONNECTED, transport: 'bluetooth' });
    assert.equal(sb.document.getElementById('radioHealthReceive').title, 'BLE-NOTE');
    assert.equal(sb.document.getElementById('restartListenerBtn').disabled, true);
}

{
    const sb = await runCard({ status: 'OK', level: 'OK', listener_running: true, transport: 'serial', packet_age: 5 });
    const get = (id) => sb.document.getElementById(id);
    assert.equal(sb._listenerRow.style.display, '');
    assert.match(get('radioHealthListener').textContent, /Running/);
    assert.equal(get('radioHealthReceiveRow').style.display, 'none');
    assert.equal(get('restartListenerBtn').disabled, false);
    assert.equal(get('restartListenerBtn').title, '');
}

console.log('test_non_serial_listener_ui: all assertions passed');
