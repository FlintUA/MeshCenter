// tests/frontend/test_files_ui.mjs
//
// Dependency-free behavior tests for static/files.js (the MCAttach Files
// workspace). This repo has no build step, no package.json, and no JS test
// framework (CI's own JS gate is `node --check` — syntax only), so rather than
// introduce jest/a browser runner as new project infrastructure, this uses
// Node's built-in `vm` to execute the REAL files.js source against a small
// stub DOM/fetch/I18N/notification surface, and Node's built-in `assert`.
// Runnable as `node tests/frontend/test_files_ui.mjs`.
//
// These are the executable counterparts to the static contract checks in
// tests/test_files_frontend_integrity.py, each mapping to a confirmed defect
// from the PR #245 verification pass:
//
//   V3  a newly discovered, unbound Meshtastic contact offers "Request key"
//       (and the local node is never offered as a contact)
//   V4  the trust-confirm dialog shows the FULL current fingerprint
//   V5  an async command that turns up 404 after a restart reports "unknown",
//       never success, and never replays the destructive request
//   V6  an async command reports success only once its status is terminal
//       "succeeded" (not on the 202 accept)
//   V7  the action matrix never offers Retry for a terminal FAILED_* sender,
//       offers Retry+Cancel for an in-flight sender, Accept+Reject for a
//       WAITING_CONSENT receiver
//   V11 the send form gates on the provider's authoritative upload-readiness
//   V15 destructive operations (revoke) require an explicit confirmation
//
// The tests drive files.js through the same delegated data-files-* click
// path the browser uses, so they exercise the dispatch guard + the real
// command tracker, not a reimplementation.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(__dirname, '..', '..', 'static', 'files.js');
const source = readFileSync(scriptPath, 'utf8');

// ---- minimal fake DOM -------------------------------------------------------

class FakeElement {
    constructor(tag, id) {
        this.tagName = String(tag || 'div').toUpperCase();
        this.id = id || '';
        this.innerHTML = '';
        this.textContent = '';
        this.disabled = false;
        this.className = '';
        this.style = {};
        this.value = '';
        this.files = [];
        this._attrs = {};
        this._children = [];
        this.parentNode = null;
        this._listeners = {};
        this._classes = new Set();
        this.dataset = {};
    }
    setAttribute(name, value) {
        this._attrs[name] = String(value);
        if (name === 'id') this.id = String(value);
        if (name.startsWith('data-')) this.dataset = this.dataset || {};
    }
    getAttribute(name) {
        if (Object.prototype.hasOwnProperty.call(this._attrs, name)) return this._attrs[name];
        if (name === 'id') return this.id || null;
        return null;
    }
    appendChild(child) { child.parentNode = this; this._children.push(child); return child; }
    removeChild(child) {
        const i = this._children.indexOf(child);
        if (i >= 0) this._children.splice(i, 1);
        child.parentNode = null;
        return child;
    }
    addEventListener(type, fn) {
        if (!this._listeners[type]) this._listeners[type] = [];
        this._listeners[type].push(fn);
    }
    querySelectorAll() { return []; }
    focus() { /* no-op */ }
    classList() { return this; }
    toggle(name, force) {
        if (force === undefined) {
            if (this._classes.has(name)) { this._classes.delete(name); return false; }
            this._classes.add(name); return true;
        }
        if (force) this._classes.add(name); else this._classes.delete(name);
        return Boolean(force);
    }
    add(name) { this._classes.add(name); }
    remove(name) { this._classes.delete(name); }
    contains(name) { return this._classes.has(name); }
}

class FakeDocument {
    constructor() {
        this.elements = new Map();
        this.listeners = {};
        this.body = new FakeElement('body', '');
        this.activeElement = null;
        this.hidden = false;
    }
    getElementById(id) {
        if (!this.elements.has(id)) this.elements.set(id, new FakeElement('div', id));
        return this.elements.get(id);
    }
    createElement(tag) { return new FakeElement(tag); }
    addEventListener(type, fn) {
        if (!this.listeners[type]) this.listeners[type] = [];
        this.listeners[type].push(fn);
    }
    querySelectorAll() { return []; }
}

// ---- sandbox -----------------------------------------------------------------

let counter = 0;
function buildSandbox({ fetchImpl }) {
    const document = new FakeDocument();
    const fetchLog = [];
    const notifications = [];   // {kind:'progress'|'update', message, type, id}
    const toasts = [];

    const wrappedFetch = async (url, options) => {
        fetchLog.push({ url, options });
        return fetchImpl(url, options);
    };

    const sandbox = {
        console,
        document,
        window: {
            I18N: {
                tOrFallback(key, params, fallback) { return fallback; },
                t(key, params) { return `[[${key}]]`; },
            },
        },
        // window.open / window.location are only reached on the download path,
        // which these tests do not exercise.
        navigator: { clipboard: { writeText: async () => {} } },
        crypto: { randomUUID: () => `test-${++counter}` },
        setTimeout,
        clearTimeout,
        Date,
        Promise,
        fetch: wrappedFetch,
        showToast: (message) => { toasts.push(message); },
        showProgressNotification: (message) => {
            const id = `notif-${++counter}`;
            notifications.push({ kind: 'progress', id, message });
            return id;
        },
        updateNotification: (id, message, type) => {
            notifications.push({ kind: 'update', id, message, type: type || 'info' });
        },
        _toasts: toasts,
        _notifications: notifications,
        _fetchLog: fetchLog,
        _document: document,
    };
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'files.js' });
    return sandbox;
}

// Dispatch a delegated click with a synthetic target carrying the given
// attributes (mirrors the browser's event-delegation entry point).
function dispatch(sandbox, attrs) {
    const target = new FakeElement('button');
    for (const [name, value] of Object.entries(attrs)) target.setAttribute(name, value);
    // Attach to a throwaway parent so closestAttr() can walk up.
    const root = new FakeElement('div');
    root.appendChild(target);
    for (const fn of sandbox._document.listeners.click || []) {
        fn({ target });
    }
}

async function waitFor(pred, { timeout = 2500, interval = 10 } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
        try {
            if (pred()) return true;
        } catch {
            // predicate touched a not-yet-rendered element; keep polling
        }
        await new Promise((r) => setTimeout(r, interval));
    }
    try {
        return pred();
    } catch {
        return false;
    }
}

function activate(sandbox, { contacts = [], nodes = [], attachments = [] } = {}) {
    sandbox.window.MeshCenterFiles.activate();
    // stubs for these are provided by the per-test fetchImpl
    return { contacts, nodes, attachments };
}

// ---- projections -------------------------------------------------------------

function attachment(id, direction, state, extra = {}) {
    return {
        id,
        direction,
        state,
        file_name: 'report.pdf',
        mime_type: 'application/pdf',
        plain_size: 4096,
        cipher_size: 4280,
        created_at: 1700000000,
        hard_expires_at: 1700259200,
        download_grace_seconds: 3600,
        provider_id: 'prov1',
        saved: false,
        content_available: true,
        primary_delivery_id: null,
        recipients: [{ key_id: '!22222222', principal_id: '!22222222' }],
        deliveries: [],
        error_code: null,
        ...extra,
    };
}

function contact(id, status, extra = {}) {
    return {
        contact_id: id,
        status,
        fingerprint: 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718',
        pending_fingerprint: 'f9e8d7c6b5a4938271605e4d3c2b1a09f8e7d6c5b4a39281',
        key_epoch: 1,
        pending_key_epoch: 2,
        ...extra,
    };
}

// ---- shared default routes for activate() ------------------------------------

function defaultRoutes(extra) {
    return async (url) => {
        if (extra) {
            const handled = await extra(url);
            if (handled !== undefined) return handled;
        }
        if (url === '/api/nodes_management') {
            return { status: 200, json: async () => ({ nodes: [], total: 0 }) };
        }
        if (url === '/api/mca/contacts') {
            return { status: 200, json: async () => ({ ok: true, contacts: [] }) };
        }
        if (url === '/api/base_status') {
            return { status: 200, json: async () => ({ node_id: '!11111111', node_name: 'Me', profile_id: 'p1' }) };
        }
        if (url.startsWith('/api/attachments?')) {
            return { status: 200, json: async () => ({ ok: true, attachments: [], total: 0 }) };
        }
        if (url === '/api/mca/connectivity') {
            return { status: 200, json: async () => ({ ok: true, internet: 'online', relays: {} }) };
        }
        if (url === '/api/settings') {
            return { status: 200, json: async () => ({ ok: true, settings: { meshtastic: { transport: 'serial' } } }) };
        }
        if (url === '/api/mca/providers') {
            return { status: 200, json: async () => ({ ok: true, providers: [] }) };
        }
        throw new Error(`unexpected fetch: ${url}`);
    };
}

function json(status, body) {
    return { status, json: async () => body };
}

// ---- tests -------------------------------------------------------------------

async function test_activate_merges_contacts_and_excludes_local() {
    // V3: a known Meshtastic node without an MCA binding must show "Request
    // key", and the local node must never be listed as a contact.
    const nodes = [
        { name: 'Bob', node_id: '!22222222', ignored: false, favorite: false, last_seen: 1 },
        { name: 'Me', node_id: '!11111111', ignored: false, favorite: false, last_seen: 1 },
    ];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/nodes_management') return json(200, { nodes, total: 2 });
            if (url === '/api/mca/contacts') return json(200, { ok: true, contacts: [] });
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('Request key'));

    const html = sandbox._document.elements.get('filesContactsList').innerHTML;
    assert.match(html, /!22222222/, 'unbound known node must appear as a contact');
    assert.match(html, /Request key/, 'unbound known node must offer "Request key"');
    assert.doesNotMatch(html, /!11111111/, 'the local node must be excluded from the contact list');

    console.log('PASS: test_activate_merges_contacts_and_excludes_local');
}

async function test_trust_confirm_shows_full_fingerprint() {
    // V4: the trust-confirm dialog must surface the COMPLETE current
    // fingerprint (grouped), not the old 16-char truncation.
    const full = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718';
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!22222222', 'confirmation_required', { name: 'Bob' })] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('Trust key'));

    dispatch(sandbox, { 'data-files-action': 'contact-confirm', 'data-contact': '!22222222' });

    // The confirm dialog is appended to document.body; find it by scanning
    // body children for the dialog root carrying the fingerprint.
    const dialogs = sandbox._document.body._children.filter((c) => (c.className || '').includes('files-dialog-root'));
    assert.ok(dialogs.length >= 1, 'confirm dialog must open on contact-confirm');
    const bodyHtml = dialogs.map((d) => d.innerHTML).join('');

    // V4: the dialog must render the FULL fingerprint grouped into 4-char runs,
    // not the old 16-char truncation.
    const grouped = full.match(/.{1,4}/g).join(' ');
    assert.ok(bodyHtml.includes(grouped), 'confirm dialog must show the full grouped fingerprint, not a 16-char prefix');

    console.log('PASS: test_trust_confirm_shows_full_fingerprint');
}

async function test_command_reports_success_only_on_terminal_succeeded() {
    // V6: a 202 accept is "queued", NOT success; success appears only once the
    // command status poll reports terminal "succeeded".
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT')], total: 1 });
            }
            if (url === '/api/attachments/a1/revoke') {
                return json(202, { ok: true, command_id: 'cmd-1' });
            }
            if (url === '/api/mca/commands/cmd-1') {
                return json(200, { ok: true, command: { command_id: 'cmd-1', status: 'succeeded', type: 'revoke', resource_id: 'a1' } });
            }
            if (url === '/api/attachments/a1') {
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT'), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('report.pdf'));

    dispatch(sandbox, { 'data-files-action': 'attach-revoke', 'data-attachment': 'a1' });
    // Confirm the destructive action.
    dispatch(sandbox, { 'data-files-action': 'modal-confirm' });

    // Queued progress first — success must NOT have been shown yet.
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'progress'));
    assert.ok(
        !sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'),
        'success must not be reported on the 202 accept, before the command status resolves',
    );

    // Then, once the poll returns succeeded, success is shown.
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'));
    console.log('PASS: test_command_reports_success_only_on_terminal_succeeded');
}

async function test_command_404_unknown_never_replays() {
    // V5: a 404 on the command-status poll (service restarted) must report
    // "unknown", never success, and never replay the destructive revoke.
    let revokeCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT')], total: 1 });
            }
            if (url === '/api/attachments/a1/revoke') {
                revokeCalls += 1;
                return json(202, { ok: true, command_id: 'cmd-404' });
            }
            if (url === '/api/mca/commands/cmd-404') {
                return json(404, { ok: false, error: 'not found', error_code: 'attachment_not_found' });
            }
            if (url === '/api/attachments/a1') {
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT'), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('report.pdf'));

    dispatch(sandbox, { 'data-files-action': 'attach-revoke', 'data-attachment': 'a1' });
    dispatch(sandbox, { 'data-files-action': 'modal-confirm' });

    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'warning'));

    assert.equal(
        sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'),
        false,
        'a 404 command result must never be reported as success',
    );
    assert.equal(revokeCalls, 1, 'the destructive revoke must never be replayed after a 404 result');

    console.log('PASS: test_command_404_unknown_never_replays');
}

async function test_action_matrix_never_offers_retry_for_terminal_failed() {
    // V7: Retry must not be offered for a terminal FAILED_* sender; it must be
    // offered (with Cancel) for an in-flight sender, and Accept+Reject for a
    // WAITING_CONSENT receiver.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                return json(200, {
                    ok: true,
                    attachments: [
                        attachment('fail', 'sent', 'FAILED_UPLOAD'),
                        attachment('sending', 'sent', 'QUEUED_UPLOAD'),
                        attachment('incoming', 'received', 'WAITING_CONSENT'),
                    ],
                    total: 3,
                });
            }
            const detailMatch = url.match(/^\/api\/attachments\/(fail|sending|incoming)$/);
            if (detailMatch) {
                const states = { fail: 'FAILED_UPLOAD', sending: 'QUEUED_UPLOAD', incoming: 'WAITING_CONSENT' };
                const dirs = { fail: 'sent', sending: 'sent', incoming: 'received' };
                const id = detailMatch[1];
                return json(200, { ok: true, attachment: attachment(id, dirs[id], states[id]), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);

    const detailBody = () => sandbox._document.elements.get('filesDetailBody').innerHTML;

    // FAILED_UPLOAD sender -> no Retry.
    dispatch(sandbox, { 'data-files-action': 'select', 'data-attachment': 'fail' });
    await waitFor(() => detailBody().includes('fail') || detailBody().includes('report.pdf'));
    assert.doesNotMatch(detailBody(), /attach-retry/, 'terminal FAILED_UPLOAD sender must not offer Retry');
    assert.doesNotMatch(detailBody(), /attach-cancel/, 'terminal FAILED_UPLOAD sender must not offer Cancel');

    // QUEUED_UPLOAD sender -> Retry + Cancel.
    dispatch(sandbox, { 'data-files-action': 'select', 'data-attachment': 'sending' });
    await waitFor(() => detailBody().includes('attach-retry'));
    assert.match(detailBody(), /attach-retry/, 'in-flight sender must offer Retry');
    assert.match(detailBody(), /attach-cancel/, 'in-flight sender must offer Cancel');

    // WAITING_CONSENT receiver -> Accept + Reject.
    dispatch(sandbox, { 'data-files-action': 'select', 'data-attachment': 'incoming' });
    await waitFor(() => detailBody().includes('attach-reject'));
    assert.match(detailBody(), /attach-download/, 'WAITING_CONSENT receiver must offer Accept');
    assert.match(detailBody(), /attach-reject/, 'WAITING_CONSENT receiver must offer Reject');

    console.log('PASS: test_action_matrix_never_offers_retry_for_terminal_failed');
}

async function test_revoke_requires_confirmation() {
    // V15: the revoke POST must not fire until the confirm dialog is accepted.
    let revokeCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT')], total: 1 });
            }
            if (url === '/api/attachments/a1/revoke') {
                revokeCalls += 1;
                return json(202, { ok: true, command_id: 'cmd-1' });
            }
            if (url === '/api/mca/commands/cmd-1') {
                return json(200, { ok: true, command: { command_id: 'cmd-1', status: 'succeeded' } });
            }
            if (url === '/api/attachments/a1') {
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT'), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('report.pdf'));

    dispatch(sandbox, { 'data-files-action': 'attach-revoke', 'data-attachment': 'a1' });

    // A confirm dialog opened, but no revoke was issued yet.
    const dialogs = sandbox._document.body._children.filter((c) => (c.className || '').includes('files-dialog-root'));
    assert.ok(dialogs.length >= 1, 'revoke must open a confirmation dialog');
    assert.equal(revokeCalls, 0, 'revoke must not POST before confirmation');

    // Accept the confirmation -> now the POST fires.
    dispatch(sandbox, { 'data-files-action': 'modal-confirm' });
    await waitFor(() => revokeCalls === 1);
    assert.equal(revokeCalls, 1, 'revoke must POST exactly once after confirmation');

    console.log('PASS: test_revoke_requires_confirmation');
}

async function test_send_readiness_gate_blocks_create() {
    // V11: the send form must consult the provider's authoritative
    // upload-readiness and refuse to create the attachment when not ready.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                return json(200, { ok: true, ready: false, reason: 'upload_token_missing', detail: {} });
            }
            return undefined;
        }),
    });

    // Pre-populate the send-form fields the submit handler reads.
    const recipient = sandbox._document.getElementById('filesSendRecipient');
    recipient.value = '!22222222';
    const provider = sandbox._document.getElementById('filesSendProvider');
    provider.value = 'prov1';
    const file = sandbox._document.getElementById('filesSendFile');
    file.files = [{ name: 'report.pdf', size: 4096, type: 'application/pdf' }];
    sandbox._document.getElementById('filesSendExpiry').value = 'default';
    sandbox._document.getElementById('filesSendComment').value = '';
    sandbox._document.getElementById('filesSendSubmit').disabled = false;

    dispatch(sandbox, { 'data-files-action': 'send-submit' });

    await waitFor(() => sandbox._toasts.some((m) => m.includes('upload token')));

    const creates = sandbox._fetchLog.filter((e) => e.url === '/api/attachments' && e.options?.method === 'POST');
    assert.equal(creates.length, 0, 'a not-ready provider must block the create POST');
    assert.ok(
        sandbox._toasts.some((m) => m.includes('upload token')),
        'the send form must surface the readiness reason',
    );

    console.log('PASS: test_send_readiness_gate_blocks_create');
}

async function main() {
    await test_activate_merges_contacts_and_excludes_local();
    await test_trust_confirm_shows_full_fingerprint();
    await test_command_reports_success_only_on_terminal_succeeded();
    await test_command_404_unknown_never_replays();
    await test_action_matrix_never_offers_retry_for_terminal_failed();
    await test_revoke_requires_confirmation();
    await test_send_readiness_gate_blocks_create();
    console.log('All files UI behavior tests passed.');
}

main()
    .catch((error) => {
        console.error('FAIL:', error);
        process.exitCode = 1;
    })
    .finally(() => {
        // The command-tracker tests intentionally leave a real setTimeout
        // poll chain running (that is the behavior under test). Exit once
        // assertions are done rather than let the process hang on it.
        process.exit(process.exitCode || 0);
    });
