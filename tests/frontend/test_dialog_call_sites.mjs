// tests/frontend/test_dialog_call_sites.mjs
//
// UI1: every call site that used to ask through a native confirm() / prompt() keeps its
// semantics on the in-app dialog: Cancel sends NOTHING, Confirm sends exactly the request
// it always sent. Runs the REAL functions from static/*.js in a vm sandbox with a stub
// appConfirm()/appPrompt() that records what was asked and answers as the test says.
// Covered: Updates "Update" (the one that blocked the .107 check), system reboot/restart/
// shutdown, Release radio (its own modal - Cancel/Confirm/duration), TCP host+port edit,
// media "delete all" (one danger dialog), schedule delete + unsaved-changes.
// `node tests/frontend/test_dialog_call_sites.mjs`

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const read = (name) => readFileSync(path.join(__dirname, '..', '..', 'static', name), 'utf8').split('\r\n').join('\n');
const plain = (value) => JSON.parse(JSON.stringify(value)); // cross-realm (vm) objects -> plain

// the source of one top-level function, up to its closing brace at column 0
function extractFunction(source, name) {
    const match = new RegExp(`^(async )?function ${name}\\(`, 'm').exec(source);
    assert.ok(match, `${name} not found`);
    const end = source.indexOf('\n}\n', match.index);
    assert.ok(end > match.index, `${name} end not found`);
    return source.slice(match.index, end + 3);
}

const STRINGS = {
    'dialog.update': 'Update', 'dialog.delete': 'Delete', 'dialog.restart': 'Restart',
    'dialog.reboot': 'Reboot', 'dialog.shutdown': 'Shut down', 'dialog.discard': 'Discard',
    'dialog.title_apply_update': 'Update MeshCenter',
    'dialog.title_restart_meshcenter': 'Restart MeshCenter',
    'dialog.title_reboot': 'Restart device', 'dialog.title_shutdown': 'Shut down device',
    'dialog.title_delete_all_images': 'Delete all images',
    'dialog.title_delete_schedule': 'Delete schedule', 'dialog.title_unsaved_changes': 'Unsaved changes',
    'system.updates_apply_confirm': 'Update MeshCenter from {current} to {latest}?',
    'system.restart_meshcenter_confirm': 'Restart MeshCenter service?',
    'system.reboot_device_confirm': 'Restart device?',
    'system.shutdown_device_confirm': 'Shut down device?',
    'media.delete_all_confirm': 'Delete ALL images?',
    'media.delete_all_confirm_count': 'Delete ALL {count} images?',
    'time.confirm_delete': 'Delete this schedule?', 'time.unsaved_changes': 'Discard unsaved changes?',
    'node_manager.edit_tcp_title': 'Edit TCP connection', 'node_manager.edit_tcp_confirm': 'Save and connect',
    'node_manager.edit_tcp_host_prompt': 'TCP host', 'node_manager.edit_tcp_port_prompt': 'TCP port',
    'node_manager.tcp_host_required': 'host required', 'node_manager.tcp_port_invalid': 'bad port',
    'node_manager.connection_switched': 'switched',
};
const I18N = {
    t: (key, params) => (STRINGS[key] ?? key).replace(/\{(\w+)\}/g, (m, n) => (params && n in params ? String(params[n]) : m)),
    plural: (key, count, params) => I18N.t(key, { count, ...params }),
};

function makeSandbox(extra = {}) {
    const asked = [];
    const fetches = [];
    const toasts = [];
    const sandbox = {
        console, setTimeout: () => 0, Date, Promise, Number, String, Math, parseInt, RegExp,
        window: null,
        asked, fetches, toasts,
        showToast: (message, type) => { toasts.push({ message, type }); },
        fetch: async (url, options) => { fetches.push({ url, options }); return { ok: true, status: 200, json: async () => ({ ok: true }) }; },
        loadRadioHealth: () => {},
        answer: true,
        appConfirm: async (spec) => { asked.push(spec); return sandbox.answer; },
        appPrompt: async (spec) => { asked.push(spec); return sandbox.answer; },
        ...extra,
    };
    sandbox.window = sandbox;
    sandbox.I18N = I18N;
    vm.createContext(sandbox);
    return sandbox;
}
const run = (sandbox, code) => vm.runInContext(code, sandbox);

// ------------------------------------------------------------ Updates: apply
{
    const updatesSource = read('chat-updates-security.js');
    const source = extractFunction(updatesSource, '_setUpdatesChecking') + ' ' + extractFunction(updatesSource, 'applyUpdate');
    for (const accept of [false, true]) {
        const sandbox = makeSandbox({
            _lastUpdatesStatus: { current_version: '1.11.0', latest_version: '1.12.0' },
            document: { getElementById: () => ({ textContent: '' }) },
            renderUpdatesStatus: () => {},
            escapeHtml: (v) => String(v),
            renderUpdatesResult: () => {},
            pollAfterUpdateRestart: () => { sandbox.polled = true; },
        });
        sandbox.answer = accept;
        sandbox.fetch = async (url, options) => {
            sandbox.fetches.push({ url, options });
            if (url === '/api/updates/preflight') return { ok: true, json: async () => ({ ok: true, dirty_files: [] }) };
            return { ok: false, status: 500, json: async () => ({ ok: false, error: 'stub' }) };
        };
        run(sandbox, source);
        const button = { disabled: false };
        await run(sandbox, 'applyUpdate')(button);
        const applied = sandbox.fetches.some((f) => f.url === '/api/updates/apply');
        assert.equal(sandbox.asked.length, 1, 'exactly one dialog');
        const spec = sandbox.asked[0];
        assert.equal(spec.danger, true, 'applying an update is a danger action');
        assert.equal(spec.title, 'Update MeshCenter');
        assert.equal(spec.message, 'Update MeshCenter from 1.11.0 to 1.12.0?');
        assert.equal(spec.confirmLabel, 'Update');
        assert.equal(applied, accept, accept ? 'Confirm sends /api/updates/apply' : 'Cancel sends nothing');
        if (!accept) assert.equal(button.disabled, false, 'Cancel re-enables the button');
        assert.ok(!sandbox.polled);
    }
}

// ------------------------------------------------------------ system actions (reboot / restart / shutdown)
{
    const source = extractFunction(read('chat.js'), 'runSystemAction');
    for (const action of ['restart_meshcenter', 'reboot', 'shutdown']) {
        for (const accept of [false, true]) {
            const sandbox = makeSandbox();
            sandbox.answer = accept;
            run(sandbox, source);
            await run(sandbox, 'runSystemAction')(action, { textContent: 'x', disabled: false });
            assert.equal(sandbox.asked.length, 1);
            assert.equal(sandbox.asked[0].danger, true, `${action} is a danger action`);
            const sent = sandbox.fetches.filter((f) => f.url === '/api/system/action');
            assert.equal(sent.length, accept ? 1 : 0, `${action} ${accept ? 'confirm' : 'cancel'}`);
            if (accept) assert.deepEqual(JSON.parse(sent[0].options.body), { action });
        }
    }
}

// ------------------------------------------------------------ Release radio (its own modal)
{
    const chat = read('chat.js');
    const source = extractFunction(chat, 'closeReleaseRadioModal') + extractFunction(chat, 'openReleaseRadioModal');
    function setup() {
        const listeners = [];
        const buttons = {};
        const options = [900, 3600, null].map((d, i) => ({
            dataset: { duration: d === null ? '' : String(d) }, classList: { toggle() {} },
            setAttribute() {}, listeners: {}, focus() {},
            addEventListener(type, fn) { this.listeners[type] = fn; },
        }));
        for (const action of ['cancel', 'confirm']) {
            buttons[action] = { disabled: false, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };
        }
        const overlay = {
            id: '', className: '', innerHTML: '', removed: false, listeners: {},
            querySelectorAll: () => options,
            querySelector(sel) {
                if (sel.includes('data-release-action="cancel"')) return buttons.cancel;
                if (sel.includes('data-release-action="confirm"')) return buttons.confirm;
                return options[0];
            },
            addEventListener(type, fn) { this.listeners[type] = fn; },
            remove() { this.removed = true; sandbox.openOverlay = null; },
        };
        const sandbox = makeSandbox({
            escapeHtml: (v) => String(v),
            releases: [],
            releaseRadioConnection: async (duration) => { sandbox.releases.push(duration); },
            openOverlay: null,
            document: {
                getElementById: (id) => (id === 'radioReleaseModal' ? sandbox.openOverlay : null),
                createElement: () => overlay,
                removeEventListener() {}, addEventListener(type, fn) { listeners.push(fn); },
                body: { appendChild(el) { sandbox.openOverlay = el; } },
            },
        });
        run(sandbox, source);
        return { sandbox, overlay, buttons, options };
    }

    // Cancel -> nothing released
    let env = setup();
    run(env.sandbox, 'openReleaseRadioModal')('tcp');
    assert.ok(env.sandbox.openOverlay, 'modal opened');
    env.buttons.cancel.listeners.click();
    assert.equal(env.sandbox.releases.length, 0);
    assert.ok(env.overlay.removed);

    // Confirm with the default (15 min)
    env = setup();
    run(env.sandbox, 'openReleaseRadioModal')('tcp');
    await env.buttons.confirm.listeners.click({ currentTarget: env.buttons.confirm });
    assert.deepEqual(plain(env.sandbox.releases), [900]);

    // Confirm with "until reconnect" (null duration)
    env = setup();
    run(env.sandbox, 'openReleaseRadioModal')('serial');
    env.options[2].listeners.click();
    await env.buttons.confirm.listeners.click({ currentTarget: env.buttons.confirm });
    assert.deepEqual(plain(env.sandbox.releases), [null]);
}

// ------------------------------------------------------------ TCP host + port: one two-field dialog
{
    const source = extractFunction(read('chat.js'), 'editRadioConnection');
    const connections = { tcp: { endpoint: { host: '192.168.2.50', port: 4403 } } };
    function build(answer) {
        const sandbox = makeSandbox({
            connected: [],
            loadNodeManagerDashboard: async () => {},
            _fetchCurrentRadioConnections: async () => connections,
            connectToSavedTransport: async (transport, endpoint) => { sandbox.connected.push([transport, endpoint]); },
        });
        sandbox.answer = answer;
        run(sandbox, source);
        return sandbox;
    }

    let sandbox = build(null); // Cancel
    await run(sandbox, 'editRadioConnection')('tcp');
    assert.equal(sandbox.connected.length, 0, 'Cancel connects nothing');
    assert.equal(sandbox.asked.length, 1, 'one dialog, not two prompts');
    const spec = sandbox.asked[0];
    assert.deepEqual(plain(spec.fields.map((f) => [f.name, f.value])), [['host', '192.168.2.50'], ['port', '4403']], 'prefilled');
    const [host, port] = spec.fields;
    assert.equal(host.validate(''), 'host required');
    assert.equal(host.validate('  '), 'host required');
    assert.equal(host.validate('radio.local'), '');
    for (const bad of ['0', '65536', 'abc', '', '4403x', '-1', '1.5']) assert.equal(port.validate(bad), 'bad port', bad);
    for (const good of ['1', '4403', '65535', ' 8080 ']) assert.equal(port.validate(good), '', good);

    sandbox = build({ host: '  radio.local ', port: ' 4404 ' }); // Confirm
    await run(sandbox, 'editRadioConnection')('tcp');
    assert.deepEqual(plain(sandbox.connected), [['tcp', { host: 'radio.local', port: 4404 }]]);

    sandbox = build(null);
    await run(sandbox, 'editRadioConnection')('bluetooth');
    assert.equal(sandbox.asked.length, 0, 'only TCP connections are editable');
}

// ------------------------------------------------------------ media: delete all = ONE danger dialog
{
    const source = extractFunction(read('media.js'), 'deleteAllMedia');
    for (const [shown, accept] of [[3, false], [3, true], [0, true]]) {
        const sandbox = makeSandbox({
            mediaGalleryLoaded: true,
            loadMediaGallery: async () => {},
            document: { querySelectorAll: () => new Array(shown).fill({}) },
        });
        sandbox.answer = accept;
        run(sandbox, source);
        await run(sandbox, 'deleteAllMedia')();
        assert.equal(sandbox.asked.length, 1, 'the chained second confirm is gone');
        assert.equal(sandbox.asked[0].danger, true);
        assert.equal(sandbox.asked[0].message, shown ? `Delete ALL ${shown} images?` : 'Delete ALL images?');
        const sent = sandbox.fetches.filter((f) => f.url === '/api/camera/screenshots' && f.options.method === 'DELETE');
        assert.equal(sent.length, accept ? 1 : 0);
    }
}

// ------------------------------------------------------------ schedules: delete + unsaved changes
{
    const chat = read('chat.js');
    const source = extractFunction(chat, 'deleteScheduleRule') + extractFunction(chat, 'closeScheduleForm');
    for (const accept of [false, true]) {
        const modal = { style: { display: 'block' } };
        const sandbox = makeSandbox({
            loadSchedules: async () => {},
            _scheduleFormDirty: true,
            scheduleEditingId: 'r1',
            document: { getElementById: (id) => (id === 'scheduleModal' ? modal : null) },
        });
        sandbox.answer = accept;
        run(sandbox, source);

        await run(sandbox, 'deleteScheduleRule')('r1');
        const deletes = sandbox.fetches.filter((f) => f.options?.method === 'DELETE');
        assert.equal(deletes.length, accept ? 1 : 0, 'delete: Cancel sends nothing');
        assert.equal(sandbox.asked[0].danger, true);

        await run(sandbox, 'closeScheduleForm')();
        assert.equal(sandbox.asked[1].danger, false, 'discarding unsaved changes is not a danger action');
        assert.equal(modal.style.display, accept ? 'none' : 'block', 'Cancel keeps the form open');
    }
}

console.log('OK - dialog call sites (Cancel sends nothing, Confirm sends the same request as before)');
