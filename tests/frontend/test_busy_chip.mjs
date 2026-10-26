// tests/frontend/test_busy_chip.mjs
//
// SIG4b: while a radio command (traceroute, scan, send...) holds the radio the header chip says
// "Busy: <reason>" (the server answers PAUSED with busy_reason), plain "Busy" without a reason, and an
// unknown reason is worded as a generic radio command - never "Offline". Runs the REAL updateHeaderNodeStatus()
// and busyReasonLabel() from static/chat.js against the REAL en.json. `node tests/frontend/test_busy_chip.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const root = path.join(__dirname, '..', '..', 'static');
const source = readFileSync(path.join(root, 'chat.js'), 'utf8').split('\r\n').join('\n');
const catalog = JSON.parse(readFileSync(path.join(root, 'i18n', 'en.json'), 'utf8'));
const lookup = (key) => key.split('.').reduce((node, part) => (node === undefined ? undefined : node[part]), catalog);
const fill = (raw, params) => raw.replace(/\{(\w+)\}/g, (m, name) => (params && name in params ? String(params[name]) : m));
const I18N = {
    t(key, params) {
        const raw = lookup(key);
        assert.ok(typeof raw === 'string', `missing en.json key: ${key}`);
        return fill(raw, params);
    },
    tOrFallback(key, params, fallback) {
        const raw = lookup(key);
        return typeof raw === 'string' ? fill(raw, params) : fallback;
    },
};

function extractFunction(name) {
    const match = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(source);
    assert.ok(match, `${name} not found`);
    return source.slice(match.index, source.indexOf('\n}\n', match.index) + 3);
}

function chip(data) {
    const label = { textContent: '' };
    const classes = new Set();
    const header = {
        querySelector: () => label,
        classList: { remove: (...names) => names.forEach((n) => classes.delete(n)), add: (n) => classes.add(n) },
        setAttribute() {},
        title: '',
    };
    const context = {
        document: { getElementById: (id) => (id === 'headerStatusText' ? header : null) },
        window: { I18N },
        formatReconnectStatus: () => '',
        formatReleasedLabel: () => 'released',
        isNonSerialTransport: () => false,
    };
    context.window.window = context.window;
    vm.createContext(context);
    vm.runInContext(['busyReasonLabel', 'updateHeaderNodeStatus'].map(extractFunction).join('\n'), context);
    context.updateHeaderNodeStatus(data, true);
    return { text: label.textContent, classes };
}

// a traceroute: "Busy: traceroute", a warning (amber), not an error
{
    const result = chip({ status: 'PAUSED', level: 'WARNING', listener_running: false, busy_reason: 'traceroute' });
    assert.equal(result.text, 'Busy: traceroute');
    assert.ok(result.classes.has('status-warning'));
}
assert.equal(chip({ status: 'PAUSED', listener_running: false, busy_reason: 'node_scan' }).text, 'Busy: node scan');
assert.equal(chip({ status: 'PAUSED', listener_running: false, busy_reason: 'something_new' }).text, 'Busy: radio command', 'unknown reason: generic');
assert.equal(chip({ status: 'PAUSED', listener_running: false }).text, 'Busy', 'no reason: plain Busy');
// the listener really down is still Offline
assert.equal(chip({ status: 'LISTENER_DOWN', level: 'ERROR', listener_running: false }).text, 'Offline');

console.log('test_busy_chip.mjs: ok');
