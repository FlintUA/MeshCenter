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

    // Escape closes the dialog and restores the background.
    dispatchKey(sandbox, 'Escape');
    assert.ok(
        !sandbox._document.body._children.some((c) => (c.className || '').includes('files-dialog-root')),
        'Escape must close the dialog',
    );
    assert.equal(main.getAttribute('aria-hidden'), null, 'background inert state must be restored on close');

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

    fileInput.files = [{ name: 'report.pdf', size: 4096, type: 'application/pdf' }];
    dispatchChange(sandbox, 'filesSendFile');
    let feedback = sandbox._document.getElementById('filesSendFileFeedback').innerHTML;
    assert.match(feedback, /application\/pdf/, 'a known extension must surface its MIME type in the advisory feedback');
    assert.match(feedback, /Size/, 'file size feedback must be present');

    // Unknown type: advisory warning, not a hard block (server is authoritative).
    fileInput.files = [{ name: 'archive.bin', size: 128, type: 'application/octet-stream' }];
    dispatchChange(sandbox, 'filesSendFile');
    feedback = sandbox._document.getElementById('filesSendFileFeedback').innerHTML;
    assert.match(feedback, /unrecognized/, 'an unrecognized type must be described as unrecognized');

    console.log('PASS: test_send_file_feedback_shows_mime');
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
    console.log('All files UI behavior tests passed (24 scenarios).');
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
