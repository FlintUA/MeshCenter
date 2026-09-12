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

// PR 4: files.js now consumes window.MeshCenterTargets (static/targets.js), the
// shared navigation target store loaded before files.js in index.html. The test
// harness mirrors that script order so files.js can resolve the store.
const targetsScriptPath = path.join(__dirname, '..', '..', 'static', 'targets.js');
const targetsSource = readFileSync(targetsScriptPath, 'utf8');

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
        // classList is a property (not a method) so `el.classList.toggle(...)`
        // works the way setFilter() uses it.
        this.classList = {
            toggle: (name, force) => {
                const on = force === undefined ? !this._classes.has(name) : Boolean(force);
                if (on) this._classes.add(name); else this._classes.delete(name);
                return on;
            },
            add: (name) => { this._classes.add(name); },
            remove: (name) => { this._classes.delete(name); },
            contains: (name) => this._classes.has(name),
        };
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
    removeAttribute(name) {
        delete this._attrs[name];
        if (name === 'id') this.id = '';
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
        this.filterTabButtons = [];
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
    querySelectorAll(selector) {
        // Only setFilter() queries the document this way; return the
        // filter-tab buttons registered by tests so aria-selected asserts run.
        if (selector === '#filesFilterTabs [data-files-filter]') return this.filterTabButtons;
        return [];
    }
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
        FormData: class {
            constructor() { this._parts = []; }
            append(key, value) { this._parts.push([key, value]); }
        },
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
    // Load order mirrors index.html: targets.js (the shared store) before files.js.
    vm.runInContext(targetsSource, sandbox, { filename: 'targets.js' });
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
        if (url === '/api/mca/key-requests') {
            return { status: 200, json: async () => ({ ok: true, key_requests: [] }) };
        }
        if (url === '/api/chats') {
            return { status: 200, json: async () => ({ chats: [], channels: [], total_unread: 0 }) };
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

// Dispatch a delegated 'change' event on the given element id (the send form
// routes <select>/<input type=file> feedback through onDocumentChange).
function dispatchChange(sandbox, id) {
    const target = new FakeElement('input');
    target.id = id;
    for (const fn of sandbox._document.listeners.change || []) fn({ target });
}

// Dispatch a delegated keyboard event (used for the Escape-close path).
function dispatchKey(sandbox, key, extra = {}) {
    for (const fn of sandbox._document.listeners.keydown || []) fn({ key, ...extra });
}

// Provider projection helper mirroring the list endpoint's serialized shape.
function provider(id, extra = {}) {
    return {
        provider_id: id,
        display_name: 'Relay ' + id,
        origin: 'https://relay.example.com',
        service_key_fingerprint: '1234567890abcdef1234567890abcdef12345678',
        kind: 'third_party',
        tls_required: true,
        upload_allowed: true,
        download_allowed: true,
        max_ciphertext_bytes: 1000000,
        is_default: false,
        enabled: true,
        min_ttl_seconds: 60,
        max_ttl_seconds: 3600,
        protocol_version: 1,
        upload_token_configured: true,
        state: 'online',
        upload_readiness: 'ready',
        ...extra,
    };
}

// Pre-populate the send form's auto-created fields (mirrors the browser state
// the submit handler reads).
function prepareSendForm(sandbox, {
    recipient = '!22222222',
    providerId = 'prov1',
    fileName = 'report.pdf',
    mime = 'application/pdf',
    size = 4096,
    expiry = 'default',
    customTtl = '',
    comment = '',
} = {}) {
    sandbox._document.getElementById('filesSendRecipient').value = recipient;
    sandbox._document.getElementById('filesSendProvider').value = providerId;
    const file = sandbox._document.getElementById('filesSendFile');
    file.files = [{ name: fileName, size, type: mime }];
    sandbox._document.getElementById('filesSendExpiry').value = expiry;
    sandbox._document.getElementById('filesSendCustomTtlSeconds').value = customTtl;
    sandbox._document.getElementById('filesSendComment').value = comment;
    sandbox._document.getElementById('filesSendSubmit').disabled = false;
}

function createRequests(sandbox) {
    return sandbox._fetchLog.filter((e) => e.url === '/api/attachments' && e.options?.method === 'POST');
}
function createMetadata(entry) {
    const parts = entry.options?.body?._parts || [];
    const meta = parts.find(([k]) => k === 'metadata');
    return meta ? JSON.parse(meta[1]) : null;
}

// Open the provider-edit dialog for a provider (requires the provider list to
// have loaded first, so the edit action's providerById() lookup succeeds).
// guardedLoad's onDone is gated on state.active, so the workspace must be
// activated before any provider fetch will populate state.providers.
async function openProviderEditFor(sandbox, id) {
    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => (sandbox._document.elements.get('filesProvidersList')?.innerHTML || '').includes('provider-edit'));
    dispatch(sandbox, { 'data-files-action': 'provider-edit', 'data-provider': id });
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

// ---- R1: confirmation rendering is safe (escaped bodyText vs trusted bodyHtml)

async function test_confirm_rendering_escapes_untrusted_text() {
    // The payload smuggles an <img onerror=...> as the untrusted node display
    // name AND the untrusted Relay provider display name. Both must be rendered
    // through esc() (literal text) — never assembled into live markup.
    const PAYLOAD = '<img src=x onerror="window.__filesXss=1">';
    const full = 'a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718';
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/nodes_management') {
                return json(200, { nodes: [{ name: PAYLOAD, node_id: '!22222222', ignored: false, favorite: false, last_seen: 1 }], total: 1 });
            }
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!22222222', 'confirmation_required', { name: PAYLOAD })] });
            }
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [provider('prov1', { display_name: PAYLOAD })] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('Trust key'));

    // (1) The trust-confirm dialog: the payload name is an untrusted substitution.
    dispatch(sandbox, { 'data-files-action': 'contact-confirm', 'data-contact': '!22222222' });
    let dialogs = sandbox._document.body._children.filter((c) => (c.className || '').includes('files-dialog-root'));
    let bodyHtml = dialogs.map((d) => d.innerHTML).join('');
    assert.match(bodyHtml, /&lt;img/, 'the untrusted node name must be escaped to literal text');
    assert.doesNotMatch(bodyHtml, /<img\b/i, 'the escaped name must not create a live <img> element');
    assert.doesNotMatch(bodyHtml, /<\w[^>]*\bonerror\b/i, 'no inline event handler may survive escaping');
    assert.match(bodyHtml, /copy-fingerprint/, 'the fingerprint copy control must still be present');
    assert.match(bodyHtml, /a1b2c3d4/, 'the fingerprint must still be rendered');

    // (2) The provider-remove confirm dialog: the payload display name again.
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => (sandbox._document.elements.get('filesProvidersList')?.innerHTML || '').includes('provider-remove'));
    dispatch(sandbox, { 'data-files-action': 'provider-remove', 'data-provider': 'prov1' });
    dialogs = sandbox._document.body._children.filter((c) => (c.className || '').includes('files-dialog-root'));
    bodyHtml = dialogs.map((d) => d.innerHTML).join('');
    assert.match(bodyHtml, /&lt;img/, 'the untrusted provider name must be escaped to literal text');
    assert.doesNotMatch(bodyHtml, /<img\b/i, 'the escaped provider name must not create a live <img> element');

    // No script ran, and the literal text is what a user would see.
    assert.equal(sandbox.window.__filesXss, undefined, 'the injected handler must never execute');

    console.log('PASS: test_confirm_rendering_escapes_untrusted_text');
}

// ---- R2: create-command settlement and the in-flight/id-retention contract

async function test_send_failed_settles_and_unlocks() {
    let createCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                return json(200, { ok: true, ready: true, reason: null });
            }
            if (url === '/api/attachments') {
                createCalls += 1;
                return json(202, { ok: true, command_id: 'cmd-fail', attachment_id: 'a1' });
            }
            if (url === '/api/mca/commands/cmd-fail') {
                return json(200, { ok: true, command: { command_id: 'cmd-fail', status: 'failed', error_code: 'relay_unreachable' } });
            }
            return undefined;
        }),
    });

    prepareSendForm(sandbox, {});
    dispatch(sandbox, { 'data-files-action': 'send-submit' });

    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'error'));
    assert.equal(createCalls, 1, 'exactly one create POST');
    assert.equal(sandbox._document.getElementById('filesSendSubmit').disabled, false, 'Send button must re-enable after failure');
    assert.ok(
        !sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'),
        'a failed create must never report success',
    );

    console.log('PASS: test_send_failed_settles_and_unlocks');
}

async function test_send_inflight_guard_blocks_double_submit() {
    let createCalls = 0;
    const readinessResolvers = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                // Defer readiness so sendInFlight stays true while we double-click.
                return new Promise((resolve) => { readinessResolvers.push(resolve); });
            }
            if (url === '/api/attachments') {
                createCalls += 1;
                return json(202, { ok: true, command_id: 'cmd-1', attachment_id: 'a1' });
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

    prepareSendForm(sandbox, {});
    dispatch(sandbox, { 'data-files-action': 'send-submit' });
    dispatch(sandbox, { 'data-files-action': 'send-submit' });   // double-click while in flight

    await waitFor(() => readinessResolvers.length === 1);
    readinessResolvers[0](json(200, { ok: true, ready: true, reason: null }));
    await waitFor(() => createCalls === 1);
    assert.equal(readinessResolvers.length, 1, 'exactly one readiness check despite the double submit');
    assert.equal(createCalls, 1, 'the in-flight guard must block a second create');

    // The single create settles (succeeded) so the button unlocks.
    await waitFor(() => sandbox._document.getElementById('filesSendSubmit').disabled === false);

    console.log('PASS: test_send_inflight_guard_blocks_double_submit');
}

async function test_send_unknown_retains_request_id() {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                return json(200, { ok: true, ready: true, reason: null });
            }
            if (url === '/api/attachments') {
                return json(202, { ok: true, command_id: 'cmd-unknown', attachment_id: null });
            }
            if (url === '/api/mca/commands/cmd-unknown') {
                return json(404, { ok: false, error: 'not found', error_code: 'attachment_not_found' });
            }
            return undefined;
        }),
    });

    prepareSendForm(sandbox, {});
    dispatch(sandbox, { 'data-files-action': 'send-submit' });
    await waitFor(() => createRequests(sandbox).length === 1);
    const firstId = createMetadata(createRequests(sandbox)[0]).client_request_id;

    // Unknown outcome keeps the form (and its client_request_id) so a retry of
    // the unchanged form reuses the SAME id for server-side dedupe.
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'warning'));
    assert.equal(sandbox._document.getElementById('filesSendSubmit').disabled, false, 'Send button must re-enable after unknown');

    dispatch(sandbox, { 'data-files-action': 'send-submit' });
    await waitFor(() => createRequests(sandbox).length === 2);
    const secondId = createMetadata(createRequests(sandbox)[1]).client_request_id;
    assert.equal(secondId, firstId, 'an unchanged form must reuse its client_request_id after an unknown outcome');

    console.log('PASS: test_send_unknown_retains_request_id');
}

// ---- R4: selected-detail in-flight guard + coalescing

async function test_detail_terminal_not_refetched_every_poll() {
    let detailCalls = 0;
    let listCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                listCalls += 1;
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'DOWNLOADED')], total: 1 });
            }
            if (url === '/api/attachments/a1') {
                detailCalls += 1;
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'DOWNLOADED'), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesDetailBody')?.innerHTML || '').includes('report.pdf'));
    assert.equal(detailCalls, 1, 'initial auto-select fetches the selected detail once');

    // A refresh re-runs the list projection; a terminal, unchanged attachment
    // must NOT trigger a second detail fetch (R4).
    dispatch(sandbox, { 'data-files-action': 'refresh' });
    await waitFor(() => listCalls >= 2);
    await new Promise((r) => setTimeout(r, 50));
    assert.equal(detailCalls, 1, 'an unchanged terminal attachment must not be re-fetched every poll');

    console.log('PASS: test_detail_terminal_not_refetched_every_poll');
}

async function test_detail_inflight_coalesces() {
    let a1Resolve;
    const a1Gate = new Promise((r) => { a1Resolve = r; });
    let a1Calls = 0;
    let a2Calls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                return json(200, { ok: true, attachments: [
                    attachment('a1', 'sent', 'SENT', { file_name: 'one.pdf' }),
                    attachment('a2', 'sent', 'SENT', { file_name: 'two.pdf' }),
                ], total: 2 });
            }
            if (url === '/api/attachments/a1') {
                a1Calls += 1;
                await a1Gate;
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT', { file_name: 'one.pdf' }), timeline: [] });
            }
            if (url === '/api/attachments/a2') {
                a2Calls += 1;
                return json(200, { ok: true, attachment: attachment('a2', 'sent', 'SENT', { file_name: 'two.pdf' }), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => a1Calls === 1);   // a1 detail request is now in flight (gated)

    // While a1 is in flight, selecting a2 must coalesce: no second concurrent
    // detail request is issued.
    dispatch(sandbox, { 'data-files-action': 'select', 'data-attachment': 'a2' });
    assert.equal(a2Calls, 0, 'a second detail request must not be issued while one is in flight');

    a1Resolve();                            // release the gated a1 request
    await waitFor(() => a2Calls === 1);     // the coalesced a2 request fires once a1 settles
    await waitFor(() => (sandbox._document.elements.get('filesDetailBody')?.innerHTML || '').includes('two.pdf'));

    assert.equal(a1Calls, 1, 'a1 detail fetched exactly once');
    assert.equal(a2Calls, 1, 'a2 detail fetched exactly once');
    assert.ok(
        !sandbox._document.elements.get('filesDetailBody').innerHTML.includes('one.pdf'),
        'the stale a1 render must not overwrite the selected a2 detail',
    );

    console.log('PASS: test_detail_inflight_coalesces');
}

// ---- R5: provider-edit PATCH only changed fields, local validation, null clears

async function test_provider_edit_patches_only_changed_fields() {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') return json(200, { ok: true, providers: [provider('prov1')] });
            if (url === '/api/mca/providers/prov1') return json(202, { ok: true, command_id: 'cmd-edit' });
            if (url === '/api/mca/commands/cmd-edit') return json(200, { ok: true, command: { command_id: 'cmd-edit', status: 'succeeded' } });
            return undefined;
        }),
    });

    await openProviderEditFor(sandbox, 'prov1');

    // Only the display name changes; every other field matches the current value.
    sandbox._document.getElementById('filesEditName-prov1').value = 'New Name';
    sandbox._document.getElementById('filesEditEnabled-prov1').checked = true;
    sandbox._document.getElementById('filesEditUpload-prov1').checked = true;
    sandbox._document.getElementById('filesEditDownload-prov1').checked = true;
    sandbox._document.getElementById('filesEditMinTtl-prov1').value = '60';
    sandbox._document.getElementById('filesEditMaxTtl-prov1').value = '3600';

    dispatch(sandbox, { 'data-files-action': 'provider-save' });

    const patch = sandbox._fetchLog.find((e) => e.url === '/api/mca/providers/prov1' && e.options?.method === 'PATCH');
    assert.ok(patch, 'provider-save must issue exactly one PATCH');
    assert.deepEqual(JSON.parse(patch.options.body), { display_name: 'New Name' }, 'the PATCH must carry only the changed fields');

    console.log('PASS: test_provider_edit_patches_only_changed_fields');
}

async function test_provider_edit_rejects_min_gt_max() {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') return json(200, { ok: true, providers: [provider('prov1')] });
            return undefined;
        }),
    });

    await openProviderEditFor(sandbox, 'prov1');

    sandbox._document.getElementById('filesEditName-prov1').value = 'Relay prov1';
    sandbox._document.getElementById('filesEditEnabled-prov1').checked = true;
    sandbox._document.getElementById('filesEditUpload-prov1').checked = true;
    sandbox._document.getElementById('filesEditDownload-prov1').checked = true;
    sandbox._document.getElementById('filesEditMinTtl-prov1').value = '1000';
    sandbox._document.getElementById('filesEditMaxTtl-prov1').value = '100';

    dispatch(sandbox, { 'data-files-action': 'provider-save' });

    const patch = sandbox._fetchLog.find((e) => e.url === '/api/mca/providers/prov1' && e.options?.method === 'PATCH');
    assert.equal(patch, undefined, 'min > max must be rejected before any PATCH is issued');
    assert.match(
        sandbox._document.getElementById('filesProviderEditError').textContent,
        /Minimum TTL/,
        'the min > max error must be surfaced locally',
    );

    console.log('PASS: test_provider_edit_rejects_min_gt_max');
}

async function test_provider_edit_clears_ttl_as_null() {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') return json(200, { ok: true, providers: [provider('prov1')] });
            if (url === '/api/mca/providers/prov1') return json(202, { ok: true, command_id: 'cmd-edit' });
            if (url === '/api/mca/commands/cmd-edit') return json(200, { ok: true, command: { command_id: 'cmd-edit', status: 'succeeded' } });
            return undefined;
        }),
    });

    await openProviderEditFor(sandbox, 'prov1');

    sandbox._document.getElementById('filesEditName-prov1').value = 'Relay prov1';
    sandbox._document.getElementById('filesEditEnabled-prov1').checked = true;
    sandbox._document.getElementById('filesEditUpload-prov1').checked = true;
    sandbox._document.getElementById('filesEditDownload-prov1').checked = true;
    sandbox._document.getElementById('filesEditMinTtl-prov1').value = '';      // cleared
    sandbox._document.getElementById('filesEditMaxTtl-prov1').value = '3600';  // unchanged

    dispatch(sandbox, { 'data-files-action': 'provider-save' });

    const patch = sandbox._fetchLog.find((e) => e.url === '/api/mca/providers/prov1' && e.options?.method === 'PATCH');
    assert.ok(patch, 'clearing a TTL bound must issue a PATCH');
    assert.deepEqual(
        JSON.parse(patch.options.body),
        { min_ttl_seconds: null },
        'a cleared TTL bound is sent as JSON null, and unchanged fields are omitted',
    );

    console.log('PASS: test_provider_edit_clears_ttl_as_null');
}

// ---- R6: modal accessibility, filter aria-selected, transfer aria-pressed

async function test_modal_accessibility_and_escape() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });

    // A fake "main content" sibling so background-inert has something to hide.
    const main = new FakeElement('main');
    sandbox._document.body.appendChild(main);

    dispatch(sandbox, { 'data-files-action': 'send' });
    const dialog = sandbox._document.body._children.find((c) => (c.className || '').includes('files-dialog-root'));
    assert.ok(dialog, 'send must open a dialog');
    assert.match(dialog.innerHTML, /role="dialog"/, 'dialog must expose role=dialog');
    assert.match(dialog.innerHTML, /aria-modal="true"/, 'dialog must be aria-modal');
    assert.match(dialog.innerHTML, /aria-labelledby="files-send-title"/, 'dialog must be labelled by its title');
    assert.match(dialog.innerHTML, /aria-live="polite"/, 'dynamic feedback regions must be polite live regions');

    // The background must be marked inert while the modal is open.
    assert.equal(main.getAttribute('aria-hidden'), 'true', 'background must be aria-hidden while a modal is open');
    assert.equal(main.inert, true, 'background must carry the native inert property while a modal is open');

    // Escape closes the dialog and restores the background.
    dispatchKey(sandbox, 'Escape');
    assert.ok(
        !sandbox._document.body._children.some((c) => (c.className || '').includes('files-dialog-root')),
        'Escape must close the dialog',
    );
    assert.equal(main.getAttribute('aria-hidden'), null, 'background inert state must be restored on close');
    assert.equal(main.inert, false, 'background native inert property must be restored on close');

    console.log('PASS: test_modal_accessibility_and_escape');
}

async function test_filter_tabs_aria_selected() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const bAll = new FakeElement('button');
    bAll.setAttribute('data-files-filter', 'all');
    const bSent = new FakeElement('button');
    bSent.setAttribute('data-files-filter', 'sent');
    sandbox._document.filterTabButtons = [bAll, bSent];

    dispatch(sandbox, { 'data-files-filter': 'sent' });

    assert.equal(bSent.getAttribute('aria-selected'), 'true', 'the active filter tab must be aria-selected');
    assert.equal(bAll.getAttribute('aria-selected'), 'false', 'inactive filter tabs must not be aria-selected');
    assert.equal(bSent.classList.contains('active'), true, 'the active filter tab must carry the active class');

    console.log('PASS: test_filter_tabs_aria_selected');
}

async function test_transfer_row_aria_pressed() {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                return json(200, { ok: true, attachments: [
                    attachment('a1', 'sent', 'SENT'),
                    attachment('a2', 'received', 'AVAILABLE'),
                ], total: 2 });
            }
            if (url === '/api/attachments/a1') {
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT'), timeline: [] });
            }
            if (url === '/api/attachments/a2') {
                return json(200, { ok: true, attachment: attachment('a2', 'received', 'AVAILABLE'), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesArchiveList')?.innerHTML || '').includes('report.pdf'));

    // Selecting a row marks exactly that row aria-pressed="true".
    dispatch(sandbox, { 'data-files-action': 'select', 'data-attachment': 'a2' });
    await waitFor(() => (sandbox._document.elements.get('filesArchiveList')?.innerHTML || '').includes('aria-pressed="true"'));

    const html = sandbox._document.elements.get('filesArchiveList').innerHTML;
    const pressedCount = (html.match(/aria-pressed="true"/g) || []).length;
    assert.equal(pressedCount, 1, 'exactly one transfer row must be aria-pressed=true (the selected row)');
    assert.match(html, /aria-pressed="false"/, 'unselected transfer rows must be aria-pressed=false');

    console.log('PASS: test_transfer_row_aria_pressed');
}

// ---- R7: custom TTL, MIME feedback, list-all-providers, provider freshness

async function test_send_dialog_renders_custom_ttl_input() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    dispatch(sandbox, { 'data-files-action': 'send' });
    const dialog = sandbox._document.body._children.find((c) => (c.className || '').includes('files-dialog-root'));
    assert.ok(dialog, 'send must open a dialog');
    assert.match(dialog.innerHTML, /id="filesSendCustomTtlSeconds"/, 'the custom TTL input must have the canonical id');
    assert.match(dialog.innerHTML, /type="number"/, 'the custom TTL input must be a number input');
    assert.match(dialog.innerHTML, /min="1"/, 'the custom TTL input must enforce a minimum of 1 second');
    assert.match(dialog.innerHTML, /step="1"/, 'the custom TTL input must step in whole seconds');

    console.log('PASS: test_send_dialog_renders_custom_ttl_input');
}

async function test_send_file_feedback_shows_mime() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    const fileInput = sandbox._document.getElementById('filesSendFile');
    const feedback = () => sandbox._document.getElementById('filesSendFileFeedback').innerHTML;

    // Allowlisted extension + MIME -> allowed, shows the type + size.
    fileInput.files = [{ name: 'report.pdf', size: 4096, type: 'application/pdf' }];
    dispatchChange(sandbox, 'filesSendFile');
    assert.match(feedback(), /application\/pdf/, 'an allowlisted extension must surface its MIME type');
    assert.match(feedback(), /Size/, 'file size feedback must be present');

    // Allowlisted extension with an empty MIME -> still allowed (log -> text/plain).
    fileInput.files = [{ name: 'notes.log', size: 512, type: '' }];
    dispatchChange(sandbox, 'filesSendFile');
    assert.match(feedback(), /text\/plain/, 'an allowlisted extension with an empty MIME must still be allowed');

    // Extensionless file with an allowlisted MIME -> allowed.
    fileInput.files = [{ name: 'blob', size: 2048, type: 'image/png' }];
    dispatchChange(sandbox, 'filesSendFile');
    assert.match(feedback(), /image\/png/, 'an allowlisted MIME must allow an extensionless file');

    // Unknown extension + unknown MIME -> hard local rejection (F1).
    fileInput.files = [{ name: 'archive.bin', size: 128, type: 'application/octet-stream' }];
    dispatchChange(sandbox, 'filesSendFile');
    assert.match(feedback(), /File type is not allowed/, 'an unallowlisted type must be rejected with feedback');

    // Rejected -> allowed clears the stale error.
    fileInput.files = [{ name: 'report.pdf', size: 4096, type: 'application/pdf' }];
    dispatchChange(sandbox, 'filesSendFile');
    assert.doesNotMatch(feedback(), /not allowed/, 'selecting an allowed file must clear a stale rejection');

    console.log('PASS: test_send_file_feedback_shows_mime');
}

async function test_send_rejects_disallowed_file_blocks_requests() {
    // F1: a disallowed file is rejected locally before any readiness or create
    // request is issued.
    let readinessCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                readinessCalls += 1;
                return json(200, { ok: true, ready: true, reason: null });
            }
            return undefined;
        }),
    });

    prepareSendForm(sandbox, { fileName: 'archive.bin', mime: 'application/octet-stream' });
    dispatch(sandbox, { 'data-files-action': 'send-submit' });
    await new Promise((r) => setTimeout(r, 30));

    assert.equal(
        sandbox._document.getElementById('filesSendFileFeedback').textContent,
        'File type is not allowed',
        'a disallowed file must be rejected with localized feedback',
    );
    assert.equal(createRequests(sandbox).length, 0, 'a disallowed file must block the create POST');
    assert.equal(readinessCalls, 0, 'a disallowed file must block the readiness request');

    console.log('PASS: test_send_rejects_disallowed_file_blocks_requests');
}

async function test_send_lists_all_providers_with_reason() {
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [
                    provider('prov1', { is_default: true }),
                    provider('prov2', { enabled: false }),
                ] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => (sandbox._document.elements.get('filesSendProvider')?.innerHTML || '').includes('value="prov1"'));

    const html = sandbox._document.elements.get('filesSendProvider').innerHTML;
    // ALL providers are listed: the ready one selectable, the non-ready one
    // present (disabled + reason) rather than silently dropped.
    assert.match(html, /value="prov1"/, 'the ready provider must be listed');
    assert.match(html, /value="prov2"/, 'the non-ready provider must also be listed');
    assert.match(html, /disabled/, 'the non-ready provider must be disabled');
    assert.match(html, /Provider is disabled/, 'the non-ready provider must show why it is disabled');
    assert.equal(sandbox._document.getElementById('filesSendProvider').value, 'prov1', 'the ready default provider is auto-selected');

    // Relay state and upload readiness are rendered as separate facts.
    const readiness = sandbox._document.getElementById('filesSendReadiness').innerHTML;
    assert.match(readiness, /Relay state/, 'relay state must be rendered');
    assert.match(readiness, /Upload: ready/, 'upload readiness must be rendered separately');

    console.log('PASS: test_send_lists_all_providers_with_reason');
}

async function test_send_custom_ttl_validation() {
    let createCalls = 0;
    let lastTtl = null;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                const m = url.match(/requested_ttl_seconds=(\d+)/);
                lastTtl = m ? parseInt(m[1], 10) : null;
                return json(200, { ok: true, ready: true, reason: null });
            }
            if (url === '/api/attachments') {
                createCalls += 1;
                return json(202, { ok: true, command_id: 'cmd-1', attachment_id: 'a1' });
            }
            if (url === '/api/mca/commands/cmd-1') {
                return json(200, { ok: true, command: { command_id: 'cmd-1', status: 'failed' } });
            }
            return undefined;
        }),
    });

    // Invalid custom TTL is rejected before any request.
    prepareSendForm(sandbox, { expiry: 'custom', customTtl: 'not-a-number' });
    dispatch(sandbox, { 'data-files-action': 'send-submit' });
    assert.ok(sandbox._toasts.some((m) => m.includes('whole number')), 'an invalid custom TTL must be rejected locally');
    assert.equal(createCalls, 0, 'an invalid custom TTL must block the create POST');

    // A valid custom TTL reaches the authoritative readiness check.
    prepareSendForm(sandbox, { expiry: 'custom', customTtl: '120' });
    dispatch(sandbox, { 'data-files-action': 'send-submit' });
    await waitFor(() => createCalls === 1);
    assert.equal(lastTtl, 120, 'the custom TTL must be used as the requested TTL');

    console.log('PASS: test_send_custom_ttl_validation');
}

// ---- lifecycle / coverage

async function test_deactivate_clears_open_dialog() {
    const sandbox = buildSandbox({ fetchImpl: defaultRoutes() });
    dispatch(sandbox, { 'data-files-action': 'send' });
    assert.ok(
        sandbox._document.body._children.some((c) => (c.className || '').includes('files-dialog-root')),
        'dialog must be open before deactivate',
    );
    sandbox.window.MeshCenterFiles.deactivate();
    assert.ok(
        !sandbox._document.body._children.some((c) => (c.className || '').includes('files-dialog-root')),
        'deactivate must close any open dialog',
    );

    console.log('PASS: test_deactivate_clears_open_dialog');
}

// ---- final-acceptance regressions (F1-F5, Section 7) -------------------------

async function test_send_proactive_readiness_exact_ttl() {
    // F2: opening the Send dialog triggers a proactive, authoritative readiness
    // request for the EXACT requested TTL (the default 3-day TTL here).
    const ttlSeen = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [provider('prov1', { is_default: true })] });
            }
            if (url.includes('upload-readiness')) {
                const m = url.match(/requested_ttl_seconds=(\d+)/);
                ttlSeen.push(m ? parseInt(m[1], 10) : null);
                return json(200, { ok: true, ready: true, reason: null });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => ttlSeen.length >= 1);

    assert.equal(ttlSeen[0], 259200, 'the proactive check must use the exact default TTL');
    const check = sandbox._document.getElementById('filesSendReadinessCheck').innerHTML;
    assert.match(check, /Upload: ready/, 'the readiness check must render ready for the exact TTL');

    console.log('PASS: test_send_proactive_readiness_exact_ttl');
}

async function test_send_readiness_stale_response_dropped() {
    // F2: a slow, older readiness response (for a superseded TTL) must be
    // dropped by the monotonic token and never overwrite the newer result.
    const pending = []; // { ttl, resolve } in request order
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [provider('prov1', { is_default: true })] });
            }
            if (url.includes('upload-readiness')) {
                const m = url.match(/requested_ttl_seconds=(\d+)/);
                const ttl = m ? parseInt(m[1], 10) : null;
                return new Promise((resolve) => { pending.push({ ttl, resolve }); });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => pending.length >= 1);

    // Switch to a valid custom TTL -> a newer readiness request for ttl=120.
    sandbox._document.getElementById('filesSendExpiry').value = 'custom';
    sandbox._document.getElementById('filesSendCustomTtlSeconds').value = '120';
    dispatchChange(sandbox, 'filesSendExpiry');
    await waitFor(() => pending.some((p) => p.ttl === 120));

    const newest = pending.filter((p) => p.ttl === 120).pop();
    const older = pending.filter((p) => p.ttl === 259200);
    assert.ok(newest && older.length >= 1, 'an older and a newer readiness request must both have fired');

    newest.resolve(json(200, { ok: true, ready: true, reason: null }));
    await waitFor(() => /Upload: ready/.test(sandbox._document.getElementById('filesSendReadinessCheck').innerHTML));

    // Every older (default-TTL) response lands after the newer one and must be
    // dropped rather than overwriting the ready result.
    older.forEach((p) => p.resolve(json(200, { ok: true, ready: false, reason: 'relay_unreachable' })));
    await new Promise((r) => setTimeout(r, 30));
    assert.match(
        sandbox._document.getElementById('filesSendReadinessCheck').innerHTML,
        /Upload: ready/,
        'a stale readiness response must not overwrite the newer result',
    );

    console.log('PASS: test_send_readiness_stale_response_dropped');
}

async function test_send_readiness_blank_custom_ttl_no_request() {
    // F2 + Section 7: a blank Custom TTL shows a localized prompt and issues no
    // readiness request (no fictitious 86400-second TTL is fabricated).
    let readinessCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [provider('prov1', { is_default: true })] });
            }
            if (url.includes('upload-readiness')) {
                readinessCalls += 1;
                return json(200, { ok: true, ready: true, reason: null });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => readinessCalls >= 1); // the default-TTL check already fired

    sandbox._document.getElementById('filesSendExpiry').value = 'custom';
    sandbox._document.getElementById('filesSendCustomTtlSeconds').value = '';
    dispatchChange(sandbox, 'filesSendExpiry');
    await new Promise((r) => setTimeout(r, 40));

    const callsAtBlank = readinessCalls;
    const check = sandbox._document.getElementById('filesSendReadinessCheck').innerHTML;
    const summary = sandbox._document.getElementById('filesSendExpirySummary').innerHTML;
    assert.match(check, /whole number/, 'a blank custom TTL must show the local prompt, not request readiness');
    assert.match(summary, /whole number/, 'the expiry summary must prompt for a value, not show a fictitious expiry');
    assert.doesNotMatch(summary, /86400/, 'the summary must never show a fabricated 86400-second expiry');

    await new Promise((r) => setTimeout(r, 40));
    assert.equal(readinessCalls, callsAtBlank, 'a blank custom TTL must not issue a readiness request');

    console.log('PASS: test_send_readiness_blank_custom_ttl_no_request');
}

async function test_send_custom_ttl_summary_local_validation() {
    // Section 7: the Custom-TTL summary validates against the provider min/max
    // locally (below-min / above-max prompts), and renders expiry+grace only
    // for a valid value.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [provider('prov1', { is_default: true, min_ttl_seconds: 60, max_ttl_seconds: 3600 })] });
            }
            if (url.includes('upload-readiness')) {
                return json(200, { ok: true, ready: true, reason: null });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => (sandbox._document.getElementById('filesSendProvider').innerHTML || '').includes('prov1'));

    const summary = () => sandbox._document.getElementById('filesSendExpirySummary').innerHTML;
    const setTtl = (v) => {
        sandbox._document.getElementById('filesSendExpiry').value = 'custom';
        sandbox._document.getElementById('filesSendCustomTtlSeconds').value = v;
        dispatchChange(sandbox, 'filesSendExpiry');
    };

    setTtl('30'); // below min (60)
    assert.match(summary(), /below the provider minimum/, 'a below-minimum TTL must be flagged locally');
    assert.doesNotMatch(summary(), /86400/, 'no fabricated expiry for a below-minimum TTL');

    setTtl('7200'); // above max (3600)
    assert.match(summary(), /above the provider maximum/, 'an above-maximum TTL must be flagged locally');

    setTtl('120'); // valid
    assert.match(summary(), /Expires/, 'a valid custom TTL must render the expiry summary');
    assert.match(summary(), /grace/, 'a valid custom TTL must render the download grace');
    assert.doesNotMatch(summary(), /provider minimum|provider maximum/, 'a valid custom TTL must not show a min/max error');

    console.log('PASS: test_send_custom_ttl_summary_local_validation');
}

async function test_send_create_success_waits_for_refresh() {
    // F5: a create that succeeds must not announce success until the
    // authoritative transfer refresh has settled, and then exactly once.
    let listResolve;
    const listGate = new Promise((r) => { listResolve = r; });
    let listCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                return json(200, { ok: true, ready: true, reason: null });
            }
            if (url === '/api/attachments') {
                return json(202, { ok: true, command_id: 'cmd-ok', attachment_id: null });
            }
            if (url === '/api/mca/commands/cmd-ok') {
                return json(200, { ok: true, command: { command_id: 'cmd-ok', status: 'succeeded' } });
            }
            if (url.startsWith('/api/attachments?')) {
                listCalls += 1;
                await listGate;
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });

    prepareSendForm(sandbox, {});
    dispatch(sandbox, { 'data-files-action': 'send-submit' });

    await waitFor(() => createRequests(sandbox).length === 1);
    await waitFor(() => listCalls >= 1); // the onSuccess refresh is in flight, held

    assert.ok(
        !sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'),
        'create success must not be announced before the transfer refresh settles',
    );

    listResolve();
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'));
    const successes = sandbox._notifications.filter((n) => n.kind === 'update' && n.type === 'success').length;
    assert.equal(successes, 1, 'create success must fire exactly once after the refresh settles');

    console.log('PASS: test_send_create_success_waits_for_refresh');
}

async function test_send_unknown_awaits_refresh() {
    // F5: a 404 (unknown after restart) must wait for the refresh to settle
    // before announcing, and must never replay the create.
    let listResolve;
    const listGate = new Promise((r) => { listResolve = r; });
    let listCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.includes('upload-readiness')) {
                return json(200, { ok: true, ready: true, reason: null });
            }
            if (url === '/api/attachments') {
                return json(202, { ok: true, command_id: 'cmd-unk', attachment_id: null });
            }
            if (url === '/api/mca/commands/cmd-unk') {
                return json(404, { ok: false, error: 'not found', error_code: 'attachment_not_found' });
            }
            if (url.startsWith('/api/attachments?')) {
                listCalls += 1;
                await listGate;
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });

    prepareSendForm(sandbox, {});
    dispatch(sandbox, { 'data-files-action': 'send-submit' });

    await waitFor(() => createRequests(sandbox).length === 1);
    await waitFor(() => listCalls >= 1); // onUnknown refresh in flight, held

    assert.ok(
        !sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'warning'),
        'the unknown warning must wait for the refresh to settle',
    );

    listResolve();
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'warning'));
    assert.equal(createRequests(sandbox).length, 1, 'unknown must never replay the create');

    console.log('PASS: test_send_unknown_awaits_refresh');
}

async function test_provider_command_awaits_refresh() {
    // F5: a provider command that succeeds must not announce success until the
    // authoritative provider refresh has settled, and then exactly once.
    let providersCalls = 0;
    let refreshResolve;
    const refreshGate = new Promise((r) => { refreshResolve = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                providersCalls += 1;
                if (providersCalls >= 2) await refreshGate;
                return json(200, { ok: true, providers: [provider('prov1')] });
            }
            if (url === '/api/mca/providers/prov1') {
                return json(202, { ok: true, command_id: 'cmd-prov' });
            }
            if (url === '/api/mca/commands/cmd-prov') {
                return json(200, { ok: true, command: { command_id: 'cmd-prov', status: 'succeeded' } });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => (sandbox._document.elements.get('filesProvidersList')?.innerHTML || '').includes('provider-toggle'));

    dispatch(sandbox, { 'data-files-action': 'provider-toggle', 'data-provider': 'prov1' });
    await waitFor(() => providersCalls >= 2); // the onSuccess refresh is in flight, held

    assert.ok(
        !sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'),
        'provider success must not be announced before the refresh settles',
    );

    refreshResolve();
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'));
    const successes = sandbox._notifications.filter((n) => n.kind === 'update' && n.type === 'success').length;
    assert.equal(successes, 1, 'provider success must fire exactly once after the refresh settles');

    console.log('PASS: test_provider_command_awaits_refresh');
}

async function test_provider_card_shows_status_fields() {
    // F3: the shared provider card must render the live status fields — upload
    // readiness, last-checked timestamp, last check result, latency, last error
    // — with numeric-only latency and a safe fallback for a null error code.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [provider('prov1', {
                    last_checked_at: 1700000000,
                    last_check_result: 'online',
                    last_latency_ms: 42,
                    last_error_code: null,
                    upload_readiness: 'ready',
                })] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => (sandbox._document.elements.get('filesProvidersList')?.innerHTML || '').includes('provider-toggle'));

    const html = sandbox._document.elements.get('filesProvidersList').innerHTML;
    assert.match(html, /Upload readiness/, 'the provider card must show upload readiness');
    assert.match(html, /Ready/, 'upload readiness must render its localized value');
    assert.match(html, /Last checked/, 'the provider card must show the last-checked timestamp');
    assert.match(html, /Last check result/, 'the provider card must show the last check result');
    assert.match(html, /Online/, 'the last check result must render its localized value');
    assert.match(html, /Latency/, 'the provider card must show latency');
    assert.match(html, /42 ms/, 'the provider card must render numeric-only latency');
    assert.match(html, /Last error/, 'the provider card must show the last error');
    assert.match(html, /—/, 'a null error code must render the em-dash fallback, not raw text');

    console.log('PASS: test_provider_card_shows_status_fields');
}

async function test_command_awaits_inflight_then_fresh_refresh() {
    // G1: when a command's terminal projection refresh is ALREADY in flight at
    // settle time, the terminal callback must JOIN that read, then issue a fresh
    // read, and only then announce success — never while the earlier read is
    // still unresolved (the old boolean guard returned an instantly-resolved
    // promise and announced success early).
    let listCalls = 0;
    let releaseInFlight;
    const inFlightGate = new Promise((r) => { releaseInFlight = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url.startsWith('/api/attachments?')) {
                listCalls += 1;
                if (listCalls === 2) await inFlightGate; // hold the in-flight refresh
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

    // Start a second transfers load (in-flight, held) before the command settles.
    dispatch(sandbox, { 'data-files-action': 'refresh' });
    await waitFor(() => listCalls >= 2);

    dispatch(sandbox, { 'data-files-action': 'attach-revoke', 'data-attachment': 'a1' });
    dispatch(sandbox, { 'data-files-action': 'modal-confirm' });

    await waitFor(() => sandbox._fetchLog.some((e) => e.url === '/api/mca/commands/cmd-1'));

    assert.ok(
        !sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'),
        'success must not be announced while the in-flight refresh is still unresolved',
    );
    assert.equal(listCalls, 2, 'the terminal callback must join the in-flight read, not announce success early');

    releaseInFlight();
    await waitFor(() => sandbox._notifications.some((n) => n.kind === 'update' && n.type === 'success'));

    const successes = sandbox._notifications.filter((n) => n.kind === 'update' && n.type === 'success').length;
    assert.equal(successes, 1, 'success must fire exactly once after the fresh read settles');
    assert.ok(listCalls >= 3, 'a fresh authoritative read must follow the joined in-flight read');

    console.log('PASS: test_command_awaits_inflight_then_fresh_refresh');
}

async function test_send_does_not_act_on_cached_providers_before_fresh() {
    // G2.1: a new Send dialog must not render/select the cached provider list,
    // nor check readiness against it, before this dialog's own fresh provider
    // response arrives — the fresh load owns the select and readiness.
    let providerCalls = 0;
    let readinessCalls = 0;
    let releaseFresh;
    const freshGate = new Promise((r) => { releaseFresh = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                providerCalls += 1;
                if (providerCalls >= 2) await freshGate; // hold the Send dialog's fresh read
                return json(200, { ok: true, providers: [provider('prov1', { is_default: true })] });
            }
            if (url.includes('upload-readiness')) {
                readinessCalls += 1;
                return json(200, { ok: true, ready: true, reason: null });
            }
            return undefined;
        }),
    });

    // Populate the cached provider list via the workspace provider dialog.
    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => (sandbox._document.elements.get('filesProvidersList')?.innerHTML || '').includes('provider-toggle'));

    // Open a Send dialog; its fresh provider read is held.
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => providerCalls >= 2);

    const sel = sandbox._document.getElementById('filesSendProvider');
    assert.equal(sel.value, '', 'the cached provider must not be pre-selected before the fresh read');
    assert.ok(sel.innerHTML.includes('Loading providers'), 'the provider select must show a loading state, not cached providers');
    assert.equal(readinessCalls, 0, 'no readiness check may run against a cached provider before the fresh read');

    releaseFresh();
    await waitFor(() => sel.value === 'prov1');
    assert.ok(readinessCalls >= 1, 'readiness is checked only after the fresh provider projection arrives');

    console.log('PASS: test_send_does_not_act_on_cached_providers_before_fresh');
}

async function test_stale_send_settings_do_not_update_new_dialog() {
    // G2.2: a settings response started for an older Send dialog must not update
    // a newer one. Dialog 1's settings fetch (bluetooth) is held and released
    // AFTER dialog 2 opens with its own serial fetch — the stale response is dropped.
    // (activate() is required so guardedLoad onDone callbacks actually run.)
    let settingsCalls = 0;
    let releaseOld;
    const oldGate = new Promise((r) => { releaseOld = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/settings') {
                settingsCalls += 1;
                // #1 = activate's workspace settings load; #2 = dialog 1's send load.
                if (settingsCalls === 2) {
                    await oldGate;
                    return json(200, { ok: true, settings: { meshtastic: { transport: 'bluetooth' } } });
                }
                return json(200, { ok: true, settings: { meshtastic: { transport: 'serial' } } });
            }
            return undefined;
        }),
    });

    activate(sandbox);

    // Dialog 1: its send settings fetch is held (will later report bluetooth).
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => settingsCalls === 2);

    // Close dialog 1 and open dialog 2, whose settings fetch resolves serial.
    dispatch(sandbox, { 'data-files-action': 'modal-close' });
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => settingsCalls === 3);
    await waitFor(() => (sandbox._document.getElementById('filesSendStatus').innerHTML || '').includes('serial'));

    // Release dialog 1's held bluetooth response — it must not overwrite dialog 2.
    releaseOld();
    await new Promise((r) => setTimeout(r, 30));

    const status = sandbox._document.getElementById('filesSendStatus').innerHTML;
    assert.doesNotMatch(status, /Bluetooth/, 'a stale older-dialog settings response must not update the newer dialog');
    assert.equal(
        sandbox._document.getElementById('filesSendSubmit').textContent,
        'Send',
        'the submit label must stay "Send" (serial), not flip to "Send without confirmation"',
    );

    console.log('PASS: test_stale_send_settings_do_not_update_new_dialog');
}

async function test_send_provider_fetch_not_suppressed_by_workspace_load() {
    // G2.3: an in-flight workspace provider load must not suppress the Send
    // dialog's required fresh provider request (distinct generation-scoped key).
    let providerCalls = 0;
    let releaseWorkspace;
    const wsGate = new Promise((r) => { releaseWorkspace = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                providerCalls += 1;
                if (providerCalls === 1) await wsGate; // hold the workspace load
                return json(200, { ok: true, providers: [provider('prov1', { is_default: true })] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => providerCalls === 1);

    // Open the Send dialog while the workspace provider load is still held.
    dispatch(sandbox, { 'data-files-action': 'send' });
    await waitFor(() => sandbox._document.getElementById('filesSendProvider').value === 'prov1');

    assert.ok(providerCalls >= 2, 'the Send dialog must issue its own fresh provider fetch despite the in-flight workspace load');
    releaseWorkspace();

    console.log('PASS: test_send_provider_fetch_not_suppressed_by_workspace_load');
}

async function test_provider_error_codes_are_localized() {
    // G3: raw provider last_error_code values must be mapped to localized labels
    // (numeric-only HTTP status, exception class names, identity codes), never
    // rendered verbatim; null falls back to the em-dash.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/providers') {
                return json(200, { ok: true, providers: [
                    provider('p1', { last_error_code: 'http_503' }),
                    provider('p2', { last_error_code: 'ConnectionError' }),
                    provider('p3', { last_error_code: 'service_public_key_mismatch' }),
                    provider('p4', { last_error_code: null }),
                ] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    dispatch(sandbox, { 'data-files-action': 'providers' });
    await waitFor(() => (sandbox._document.elements.get('filesProvidersList')?.innerHTML || '').includes('provider-toggle'));

    const html = sandbox._document.elements.get('filesProvidersList').innerHTML;
    assert.match(html, /HTTP 503/, 'http_503 must render as a numeric-only HTTP status label');
    assert.doesNotMatch(html, /http_503/, 'the raw http_503 code must not be rendered verbatim');
    assert.match(html, /Connection failed/, 'ConnectionError must render its localized label');
    assert.doesNotMatch(html, /ConnectionError/, 'the raw ConnectionError class name must not be rendered verbatim');
    assert.match(html, /Provider key mismatch/, 'service_public_key_mismatch must render its localized label');
    assert.match(html, /—/, 'a null error code must render the em-dash fallback');

    console.log('PASS: test_provider_error_codes_are_localized');
}

async function test_counterparty_filter_out_of_order_responses() {
    // P3: switching counterparty A -> B issues distinct fetches, and a stale A
    // response that resolves AFTER B must be dropped — never overwrite B's list.
    let releaseA;
    const gateA = new Promise((r) => { releaseA = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [
                    contact('!aaaaaaaa', 'trusted'),
                    contact('!bbbbbbbb', 'trusted'),
                ] });
            }
            if (url.startsWith('/api/attachments?')) {
                const cp = new URL(url, 'http://x').searchParams.get('counterparty');
                if (cp === '!aaaaaaaa') {
                    await gateA; // hold A's (soon-to-be-stale) response
                    return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'from-a.pdf' })], total: 1 });
                }
                if (cp === '!bbbbbbbb') {
                    return json(200, { ok: true, attachments: [attachment('b1', 'sent', 'SENT', { file_name: 'from-b.pdf' })], total: 1 });
                }
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!bbbbbbbb' });

    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-b.pdf'));

    releaseA();
    await new Promise((r) => setTimeout(r, 50));

    const html = sandbox._document.elements.get('filesArchiveList').innerHTML;
    assert.ok(html.includes('from-b.pdf'), 'B list must still be shown after A resolves late');
    assert.ok(!html.includes('from-a.pdf'), 'stale A response must not overwrite B list');

    console.log('PASS: test_counterparty_filter_out_of_order_responses');
}

async function test_counterparty_filter_a_b_a_switching() {
    // P3 (fast-switch): A -> B -> A with the FIRST A request still in flight.
    // The three dispatches happen back-to-back without waiting for any response
    // (the old test waited for each response, so it never exercised the fast
    // scenario). B resolves first while the query is already back to A and must
    // be dropped; the held A response (still the current query) must win. The
    // second A joins the still-in-flight first A rather than issuing a
    // duplicate request (query-keyed guard).
    let releaseA;
    const gateA = new Promise((r) => { releaseA = r; });
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [
                    contact('!aaaaaaaa', 'trusted'),
                    contact('!bbbbbbbb', 'trusted'),
                ] });
            }
            if (url.startsWith('/api/attachments?')) {
                const cp = new URL(url, 'http://x').searchParams.get('counterparty');
                cps.push(cp);
                if (cp === '!aaaaaaaa') {
                    await gateA; // hold the first A response across the B/back-to-A switches
                    return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'from-a.pdf' })], total: 1 });
                }
                if (cp === '!bbbbbbbb') {
                    return json(200, { ok: true, attachments: [attachment('b1', 'sent', 'SENT', { file_name: 'from-b.pdf' })], total: 1 });
                }
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    // Fast switch: dispatch all three without awaiting any response. The first
    // A fetch is gated (in flight) while B and the back-to-A switch happen.
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!bbbbbbbb' });
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });

    // Let B's fast response resolve while the query is already back to A — it
    // must be dropped and never paint B's list.
    await new Promise((r) => setTimeout(r, 30));
    assert.ok(
        !sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-b.pdf'),
        'B response must be dropped when the query is already back to A',
    );

    // Release the held first-A response; it is still the current query and wins.
    releaseA();
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-a.pdf'));

    const html = sandbox._document.elements.get('filesArchiveList').innerHTML;
    assert.ok(html.includes('from-a.pdf'), 'the held A response must win (final A selection)');
    assert.ok(!html.includes('from-b.pdf'), 'B list must not survive');

    // The second A joined the still-in-flight first A (same query key), so no
    // duplicate A request was issued — exactly one request per distinct query.
    assert.deepEqual(
        cps.filter(Boolean),
        ['!aaaaaaaa', '!bbbbbbbb'],
        'fast A->B->A issues one request per distinct query; the second A joins the first',
    );

    console.log('PASS: test_counterparty_filter_a_b_a_switching');
}

async function test_counterparty_filter_refresh_keeps_filter() {
    // P3: a manual refresh while a counterparty filter is active must re-fetch
    // WITH the counterparty still applied (never silently drop it).
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                cps.push(new URL(url, 'http://x').searchParams.get('counterparty'));
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT')], total: 1 });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => cps.includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'refresh' });
    await waitFor(() => cps.filter((c) => c === '!aaaaaaaa').length >= 2);

    assert.ok(
        cps.filter(Boolean).every((c) => c === '!aaaaaaaa'),
        'after a filter is active, every transfers request (including refresh) must carry it',
    );

    console.log('PASS: test_counterparty_filter_refresh_keeps_filter');
}

async function test_counterparty_filter_reset_returns_full_list() {
    // P3: toggling the active contact off clears the filter and returns the
    // full list (request carries no counterparty).
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                cps.push(new URL(url, 'http://x').searchParams.get('counterparty'));
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT')], total: 1 });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => cps.includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => cps.length >= 3 && cps[cps.length - 1] === null);

    assert.equal(cps[cps.length - 1], null, 'reset must issue a request with no counterparty (full list)');

    const html = sandbox._document.elements.get('filesContactsList').innerHTML;
    assert.ok(!html.includes('is-filtered'), 'reset must clear the is-filtered class');

    console.log('PASS: test_counterparty_filter_reset_returns_full_list');
}

async function test_counterparty_detail_invalidation_on_switch_to_empty() {
    // P3 review: switching counterparty while a detail fetch is still in flight
    // must invalidate it — a late detail response for the OLD counterparty can
    // never paint into the new (empty) view.
    let releaseDetail;
    const gateDetail = new Promise((r) => { releaseDetail = r; });
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [
                    contact('!aaaaaaaa', 'trusted'),
                    contact('!bbbbbbbb', 'trusted'),
                ] });
            }
            if (url === '/api/attachments?counterparty=!aaaaaaaa') {
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'from-a.pdf' })], total: 1 });
            }
            if (url === '/api/attachments?counterparty=!bbbbbbbb') {
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            if (url === '/api/attachments/a1') {
                await gateDetail; // hold the OLD counterparty's detail fetch
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT', { file_name: 'a-detail.pdf' }), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    // Select A -> its first transfer is auto-selected and its detail fetch begins (held).
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-a.pdf'));

    // Switch to B (no transfers) -> the in-flight A detail is invalidated and
    // the panel is cleared immediately.
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!bbbbbbbb' });
    await waitFor(() => !sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-a.pdf'));

    const body = sandbox._document.elements.get('filesDetailBody');
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'detail panel must be cleared on switch to empty B');

    // Release the stale A detail response — it must be dropped, not painted.
    releaseDetail();
    await new Promise((r) => setTimeout(r, 30));
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'late A detail must not paint after switching to empty B');

    console.log('PASS: test_counterparty_detail_invalidation_on_switch_to_empty');
}

async function test_counterparty_switch_to_nonempty_selects_first_card() {
    // P3 review: switching to a NON-empty counterparty must select the FIRST
    // card of the new result and show only its detail — never the previous
    // counterparty's detail, and never a non-first card.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [
                    contact('!aaaaaaaa', 'trusted'),
                    contact('!bbbbbbbb', 'trusted'),
                ] });
            }
            if (url.startsWith('/api/attachments?')) {
                const cp = new URL(url, 'http://x').searchParams.get('counterparty');
                if (cp === '!aaaaaaaa') {
                    return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'from-a.pdf' })], total: 1 });
                }
                if (cp === '!bbbbbbbb') {
                    return json(200, { ok: true, attachments: [
                        attachment('b1', 'sent', 'SENT', { file_name: 'from-b1.pdf' }),
                        attachment('b2', 'sent', 'SENT', { file_name: 'from-b2.pdf' }),
                    ], total: 2 });
                }
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            if (url === '/api/attachments/a1') {
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT', { file_name: 'a-detail.pdf' }), timeline: [] });
            }
            if (url === '/api/attachments/b1') {
                return json(200, { ok: true, attachment: attachment('b1', 'sent', 'SENT', { file_name: 'b1-detail.pdf' }), timeline: [] });
            }
            if (url === '/api/attachments/b2') {
                return json(200, { ok: true, attachment: attachment('b2', 'sent', 'SENT', { file_name: 'b2-detail.pdf' }), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    // Select A -> its single transfer is auto-selected and its detail fetched.
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => (sandbox._document.elements.get('filesDetailBody')?.innerHTML || '').includes('a-detail.pdf'));

    // Switch to B (two transfers) -> the FIRST card (b1) must be selected.
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!bbbbbbbb' });
    await waitFor(() => (sandbox._document.elements.get('filesDetailBody')?.innerHTML || '').includes('b1-detail.pdf'));

    const listHtml = sandbox._document.elements.get('filesArchiveList').innerHTML;
    assert.ok(listHtml.includes('from-b1.pdf'), 'B list shows its first transfer');
    assert.ok(listHtml.includes('from-b2.pdf'), 'B list shows its second transfer');
    assert.ok(!listHtml.includes('from-a.pdf'), 'A list must not survive the switch to B');

    const detailHtml = sandbox._document.elements.get('filesDetailBody').innerHTML;
    assert.ok(detailHtml.includes('b1-detail.pdf'), 'the first B card must be selected and its detail shown');
    assert.ok(!detailHtml.includes('b2-detail.pdf'), 'a non-first B card must not be auto-selected');
    assert.ok(!detailHtml.includes('a-detail.pdf'), 'the previous A detail must not survive');

    console.log('PASS: test_counterparty_switch_to_nonempty_selects_first_card');
}

async function test_contact_select_and_key_actions_are_separate() {
    // P3 review: the contact select area and the key-management buttons are
    // distinct sibling interactive elements — the outer item is inert, the
    // name/id block is its own button, and "Request key" is a sibling button.
    // Enter/Space on a native button maps to a click on that button, so:
    // Enter/Space on the select toggles the filter; Enter/Space on "Request
    // key" must NOT change the filter.
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'key_unknown')] });
            }
            if (url.startsWith('/api/attachments?')) {
                cps.push(new URL(url, 'http://x').searchParams.get('counterparty'));
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT')], total: 1 });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('files-contact-select'));

    const contactsHtml = sandbox._document.elements.get('filesContactsList').innerHTML;
    assert.ok(contactsHtml.includes('class="files-contact-select"'), 'the select area is a real button');
    assert.ok(!contactsHtml.includes('files-contact-item" role="button"'), 'the outer item is no longer role=button');
    assert.ok(!contactsHtml.includes('tabindex='), 'the outer item is out of the tab order (native buttons only)');
    assert.ok(contactsHtml.includes('contact-request-key'), 'the Request key button is rendered');
    // The Request key button is a sibling of the select button (outside it):
    // it appears after the select button's closing tag.
    const selectClose = contactsHtml.indexOf('</button>');
    assert.ok(
        selectClose !== -1 && contactsHtml.indexOf('contact-request-key') > selectClose,
        'Request key must be a sibling, outside the select button',
    );

    // Enter/Space on the select area == click -> toggles the filter.
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => cps.includes('!aaaaaaaa'));

    // Enter/Space on Request key == click -> must NOT change the filter (no
    // transfers request is issued).
    const before = cps.length;
    dispatch(sandbox, { 'data-files-action': 'contact-request-key', 'data-contact': '!aaaaaaaa' });
    await new Promise((r) => setTimeout(r, 30));
    assert.equal(cps.length, before, 'Request key must not issue a transfers request (filter unchanged)');

    console.log('PASS: test_contact_select_and_key_actions_are_separate');
}

// ---- PR 4 Finding 1: shared-store selection wiring (integration) -----------

async function test_contact_click_routes_through_shared_store() {
    // Finding 1: a contact click goes through the ONE shared store, not a
    // files-local counterparty set directly. The store owns the selection,
    // files.js mirrors it into `state.counterparty` (observable via the
    // counterparty query param), and the contact row's aria-pressed reflects
    // the store's selection.
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                cps.push(new URL(url, 'http://x').searchParams.get('counterparty'));
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });
    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });

    const store = sandbox.window.MeshCenterTargets;
    const sel = store.selected();
    assert.ok(sel, 'a contact click must leave a store selection');
    assert.equal(sel.kind, 'node', 'the store selection is a node target');
    assert.equal(sel.id, '!aaaaaaaa', 'the store selection is the clicked node');
    await waitFor(() => cps[cps.length - 1] === '!aaaaaaaa');
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('aria-pressed="true"'));

    console.log('PASS: test_contact_click_routes_through_shared_store');
}

async function test_second_contact_click_deselects_returns_full_list() {
    // Finding 1: a second click on the already-selected contact deselects it
    // (store selection cleared) and returns the FULL transfer list (no
    // counterparty filter), with aria-pressed back to "false".
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                cps.push(new URL(url, 'http://x').searchParams.get('counterparty'));
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });
    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });
    await waitFor(() => cps.includes('!aaaaaaaa'));

    // Second click on the SAME contact deselects.
    dispatch(sandbox, { 'data-files-action': 'contact-filter', 'data-contact': '!aaaaaaaa' });

    assert.equal(sandbox.window.MeshCenterTargets.selected(), null, 'a second click must clear the store selection');
    await waitFor(() => cps[cps.length - 1] === null);
    await waitFor(() => {
        const html = sandbox._document.elements.get('filesContactsList')?.innerHTML || '';
        return html.includes('!aaaaaaaa') && !html.includes('aria-pressed="true"');
    });

    console.log('PASS: test_second_contact_click_deselects_returns_full_list');
}

async function test_channel_selection_clears_node_counterparty() {
    // Finding 1: a channel selected in the shared store STAYS selected (the
    // store retains it, with can_send_file=false) but is never a file
    // counterparty — Files clears any prior node counterparty rather than
    // keeping the stale filter.
    const cps = [];
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                cps.push(new URL(url, 'http://x').searchParams.get('counterparty'));
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            return undefined;
        }),
    });
    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    // Select a node -> counterparty filter applies.
    sandbox.window.MeshCenterTargets.toggleSelect('node', '!aaaaaaaa');
    await waitFor(() => cps.includes('!aaaaaaaa'));

    // Select a channel -> the store keeps the channel, Files clears the filter.
    sandbox.window.MeshCenterTargets.toggleSelect('channel', 'LongFast');

    const store = sandbox.window.MeshCenterTargets;
    assert.equal(store.selected().kind, 'channel', 'the channel selection is retained by the store');
    assert.equal(store.selected().id, 'longfast', 'the channel id is canonicalized (lowercased)');
    await waitFor(() => cps[cps.length - 1] === null);
    await waitFor(() => {
        const html = sandbox._document.elements.get('filesContactsList')?.innerHTML || '';
        return !html.includes('aria-pressed="true"');
    });

    console.log('PASS: test_channel_selection_clears_node_counterparty');
}

async function test_send_dialog_preselects_valid_node() {
    // Finding 1: the Send dialog pre-selects a recipient only when the shared
    // store's selection is a valid, still-sendable node — here a trusted node.
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [
                    contact('!aaaaaaaa', 'trusted'),
                    contact('!bbbbbbbb', 'trusted'),
                ] });
            }
            return undefined;
        }),
    });
    activate(sandbox);
    await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));

    sandbox.window.MeshCenterTargets.toggleSelect('node', '!aaaaaaaa');

    dispatch(sandbox, { 'data-files-action': 'send' });

    const sel = sandbox._document.getElementById('filesSendRecipient');
    assert.equal(sel.value, '!aaaaaaaa', 'the selected valid node must be pre-selected');
    assert.ok(!sel.innerHTML.includes('Choose a trusted contact'), 'no empty placeholder when a valid node is pre-selected');

    console.log('PASS: test_send_dialog_preselects_valid_node');
}

async function test_send_dialog_no_fallback_for_invalid_channel_disappeared() {
    // Finding 1: an invalid / disappeared / channel selection must NOT silently
    // fall back to another recipient. The recipient select shows an explicit
    // empty placeholder and no value is pre-selected.
    const contactsResp = [
        contact('!aaaaaaaa', 'trusted'),
        contact('!bbbbbbbb', 'confirmation_required'), // present but NOT sendable
    ];
    const build = () => buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') return json(200, { ok: true, contacts: contactsResp });
            return undefined;
        }),
    });

    // (a) a channel selection: a channel is never a file recipient.
    {
        const sandbox = build();
        activate(sandbox);
        await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));
        sandbox.window.MeshCenterTargets.toggleSelect('channel', 'LongFast');
        assert.equal(sandbox.window.MeshCenterTargets.selected().kind, 'channel', 'channel selection is retained');
        dispatch(sandbox, { 'data-files-action': 'send' });
        const sel = sandbox._document.getElementById('filesSendRecipient');
        assert.equal(sel.value, '', 'a channel selection must not pre-select a recipient');
        assert.ok(sel.innerHTML.includes('Choose a trusted contact'), 'a channel selection shows the empty placeholder');
    }

    // (b) a selected node that is present but NOT sendable (confirmation_required).
    {
        const sandbox = build();
        activate(sandbox);
        await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));
        sandbox.window.MeshCenterTargets.toggleSelect('node', '!bbbbbbbb');
        dispatch(sandbox, { 'data-files-action': 'send' });
        const sel = sandbox._document.getElementById('filesSendRecipient');
        assert.equal(sel.value, '', 'a non-sendable node must not be pre-selected');
        assert.ok(sel.innerHTML.includes('Choose a trusted contact'), 'a non-sendable node shows the empty placeholder');
    }

    // (c) a selected node that has disappeared (not in the contact list at all).
    {
        const sandbox = build();
        activate(sandbox);
        await waitFor(() => (sandbox._document.elements.get('filesContactsList')?.innerHTML || '').includes('!aaaaaaaa'));
        sandbox.window.MeshCenterTargets.toggleSelect('node', '!cccccccc');
        dispatch(sandbox, { 'data-files-action': 'send' });
        const sel = sandbox._document.getElementById('filesSendRecipient');
        assert.equal(sel.value, '', 'a disappeared node must not pre-select a recipient');
        assert.ok(sel.innerHTML.includes('Choose a trusted contact'), 'a disappeared node shows the empty placeholder');
    }

    console.log('PASS: test_send_dialog_no_fallback_for_invalid_channel_disappeared');
}

async function test_same_query_detail_invalidation_on_empty_poll() {
    // P3 review (second pass): the list can change under the SAME query key —
    // the selected transfer completed and left the pending filter, was deleted,
    // or the server returned an empty list — with no direction/filter/
    // counterparty switch. That selection change (to null) must invalidate the
    // in-flight detail too, so a late old-detail response cannot repaint a
    // transfer that is no longer shown.
    let releaseDetail;
    const gateDetail = new Promise((r) => { releaseDetail = r; });
    let poll = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                poll += 1;
                if (poll === 1) {
                    return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'from-a.pdf' })], total: 1 });
                }
                return json(200, { ok: true, attachments: [], total: 0 });
            }
            if (url === '/api/attachments/a1') {
                await gateDetail; // hold the old selection's detail fetch across the poll
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT', { file_name: 'a-detail.pdf' }), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-a.pdf'));

    // Same-query poll now returns empty — the selection disappears without any
    // query-key change.
    dispatch(sandbox, { 'data-files-action': 'refresh' });
    await waitFor(() => !sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-a.pdf'));

    const body = sandbox._document.elements.get('filesDetailBody');
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'detail panel must be cleared when the selection disappears');

    // Release the stale detail — it must be dropped, not painted.
    releaseDetail();
    await new Promise((r) => setTimeout(r, 30));
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'late detail for the now-missing transfer must not appear');

    console.log('PASS: test_same_query_detail_invalidation_on_empty_poll');
}

async function test_same_query_replaces_selection_no_transient_stale_detail() {
    // P3 review (second pass): the same query key replacing A with B (A left the
    // pending filter and B became the first row) is a selection change, not a
    // query change. It must invalidate A's in-flight detail so A's late response
    // never paints — not even transiently — while B's coalesced detail is still
    // pending.
    let releaseDetailA, releaseDetailB;
    const gateDetailA = new Promise((r) => { releaseDetailA = r; });
    const gateDetailB = new Promise((r) => { releaseDetailB = r; });
    let poll = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                poll += 1;
                if (poll === 1) {
                    return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'from-a.pdf' })], total: 1 });
                }
                return json(200, { ok: true, attachments: [attachment('b1', 'sent', 'SENT', { file_name: 'from-b.pdf' })], total: 1 });
            }
            if (url === '/api/attachments/a1') {
                await gateDetailA;
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT', { file_name: 'a-detail.pdf' }), timeline: [] });
            }
            if (url === '/api/attachments/b1') {
                await gateDetailB;
                return json(200, { ok: true, attachment: attachment('b1', 'sent', 'SENT', { file_name: 'b-detail.pdf' }), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('from-a.pdf'));

    // Same query now replaces A with B (same direction/filter/counterparty).
    dispatch(sandbox, { 'data-files-action': 'refresh' });
    await waitFor(() => {
        const h = sandbox._document.elements.get('filesArchiveList').innerHTML;
        return h.includes('from-b.pdf') && !h.includes('from-a.pdf');
    });

    const body = sandbox._document.elements.get('filesDetailBody');
    // While B's detail is still pending (A's detail is still in flight and held),
    // the panel must be empty — not showing A's stale detail.
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'panel must be cleared while the new selection is pending');

    // Release the late A detail: it must be dropped, and the coalesced B detail
    // fires next — A's content must never paint, even transiently.
    releaseDetailA();
    await new Promise((r) => setTimeout(r, 30));
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'late A detail must not paint even transiently');
    assert.ok(!body.innerHTML.includes('b-detail.pdf'), 'B detail is still in flight (gated), not yet painted');

    releaseDetailB();
    await waitFor(() => body.innerHTML.includes('b-detail.pdf'));
    assert.ok(!body.innerHTML.includes('a-detail.pdf'), 'the stale A detail must never have painted');

    console.log('PASS: test_same_query_replaces_selection_no_transient_stale_detail');
}

async function test_reactivation_issues_fresh_transfers_request() {
    // P3 review (second pass): deactivate() bumps the epoch but does not clear
    // unfinished state.loading entries. On rapid re-entry, loadTransfers() must
    // NOT join the old-epoch in-flight request (which would be dropped on epoch
    // mismatch, leaving the fresh activation with no data until the next timer
    // tick). Including the epoch in the guard key forces a fresh request.
    let releaseOld;
    const gateOld = new Promise((r) => { releaseOld = r; });
    let transfersCalls = 0;
    const sandbox = buildSandbox({
        fetchImpl: defaultRoutes(async (url) => {
            if (url === '/api/mca/contacts') {
                return json(200, { ok: true, contacts: [contact('!aaaaaaaa', 'trusted')] });
            }
            if (url.startsWith('/api/attachments?')) {
                transfersCalls += 1;
                if (transfersCalls === 1) {
                    await gateOld; // hold the FIRST activation's transfers request
                    return json(200, { ok: true, attachments: [attachment('old1', 'sent', 'SENT', { file_name: 'stale.pdf' })], total: 1 });
                }
                return json(200, { ok: true, attachments: [attachment('a1', 'sent', 'SENT', { file_name: 'fresh.pdf' })], total: 1 });
            }
            if (url === '/api/attachments/a1') {
                return json(200, { ok: true, attachment: attachment('a1', 'sent', 'SENT', { file_name: 'fresh-detail.pdf' }), timeline: [] });
            }
            if (url === '/api/attachments/old1') {
                return json(200, { ok: true, attachment: attachment('old1', 'sent', 'SENT', { file_name: 'stale-detail.pdf' }), timeline: [] });
            }
            return undefined;
        }),
    });

    activate(sandbox);
    await waitFor(() => transfersCalls === 1); // first (old-epoch) request is now in flight and held

    // Deactivate then immediately re-activate while the first request is still held.
    sandbox.window.MeshCenterFiles.deactivate();
    sandbox.window.MeshCenterFiles.activate();
    await waitFor(() => transfersCalls >= 2);

    assert.ok(transfersCalls >= 2, 're-activation must issue a second, fresh transfers request (new epoch key)');

    // The new-epoch response applies.
    await waitFor(() => sandbox._document.elements.get('filesArchiveList').innerHTML.includes('fresh.pdf'));

    // The late old-epoch response changes nothing (dropped by epoch mismatch).
    releaseOld();
    await new Promise((r) => setTimeout(r, 30));
    const listHtml = sandbox._document.elements.get('filesArchiveList').innerHTML;
    assert.ok(listHtml.includes('fresh.pdf'), 'the fresh-epoch list must survive the late old response');
    assert.ok(!listHtml.includes('stale.pdf'), 'the late old-epoch list must not apply');

    console.log('PASS: test_reactivation_issues_fresh_transfers_request');
}

async function main() {
    await test_activate_merges_contacts_and_excludes_local();
    await test_trust_confirm_shows_full_fingerprint();
    await test_command_reports_success_only_on_terminal_succeeded();
    await test_command_404_unknown_never_replays();
    await test_action_matrix_never_offers_retry_for_terminal_failed();
    await test_revoke_requires_confirmation();
    await test_send_readiness_gate_blocks_create();
    await test_confirm_rendering_escapes_untrusted_text();
    await test_send_failed_settles_and_unlocks();
    await test_send_inflight_guard_blocks_double_submit();
    await test_send_unknown_retains_request_id();
    await test_detail_terminal_not_refetched_every_poll();
    await test_detail_inflight_coalesces();
    await test_provider_edit_patches_only_changed_fields();
    await test_provider_edit_rejects_min_gt_max();
    await test_provider_edit_clears_ttl_as_null();
    await test_modal_accessibility_and_escape();
    await test_filter_tabs_aria_selected();
    await test_transfer_row_aria_pressed();
    await test_send_dialog_renders_custom_ttl_input();
    await test_send_file_feedback_shows_mime();
    await test_send_lists_all_providers_with_reason();
    await test_send_custom_ttl_validation();
    await test_deactivate_clears_open_dialog();
    // Final-acceptance regressions (F1-F5, Section 7).
    await test_send_rejects_disallowed_file_blocks_requests();
    await test_send_proactive_readiness_exact_ttl();
    await test_send_readiness_stale_response_dropped();
    await test_send_readiness_blank_custom_ttl_no_request();
    await test_send_custom_ttl_summary_local_validation();
    await test_send_create_success_waits_for_refresh();
    await test_send_unknown_awaits_refresh();
    await test_provider_command_awaits_refresh();
    await test_provider_card_shows_status_fields();
    // Final race corrections (G1-G3).
    await test_command_awaits_inflight_then_fresh_refresh();
    await test_send_does_not_act_on_cached_providers_before_fresh();
    await test_stale_send_settings_do_not_update_new_dialog();
    await test_send_provider_fetch_not_suppressed_by_workspace_load();
    await test_provider_error_codes_are_localized();
    // PR 3: counterparty filtering + race-safe transfer loading.
    await test_counterparty_filter_out_of_order_responses();
    await test_counterparty_filter_a_b_a_switching();
    await test_counterparty_filter_refresh_keeps_filter();
    await test_counterparty_filter_reset_returns_full_list();
    await test_counterparty_detail_invalidation_on_switch_to_empty();
    await test_counterparty_switch_to_nonempty_selects_first_card();
    await test_contact_select_and_key_actions_are_separate();
    // PR 4 Finding 1: shared-store selection wiring.
    await test_contact_click_routes_through_shared_store();
    await test_second_contact_click_deselects_returns_full_list();
    await test_channel_selection_clears_node_counterparty();
    await test_send_dialog_preselects_valid_node();
    await test_send_dialog_no_fallback_for_invalid_channel_disappeared();
    await test_same_query_detail_invalidation_on_empty_poll();
    await test_same_query_replaces_selection_no_transient_stale_detail();
    await test_reactivation_issues_fresh_transfers_request();
    console.log('All files UI behavior tests passed (53 scenarios).');
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
