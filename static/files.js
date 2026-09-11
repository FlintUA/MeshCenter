/* static/files.js
 *
 * The Files workspace — MeshCenter's end-to-end encrypted file-transfer UI
 * (MCAttach). Loaded before chat.js so chat.js's `switchMainTab('files')`
 * can call `MeshCenterFiles.activate()` with a `typeof` guard (the same
 * pattern as media.js / chat-camera.js). No build step: a plain script that
 * installs exactly one public module object plus a small set of static
 * compatibility entry points.
 *
 * Backend contract (api/api_attachments.py):
 *   - GET  /api/mca/contacts                          -> contacts + trust state
 *   - GET  /api/nodes_management                      -> general Meshtastic nodes
 *   - GET  /api/base_status                           -> local node identity
 *   - GET  /api/mca/connectivity                      -> internet + per-relay state
 *   - GET  /api/settings                              -> Meshtastic transport
 *   - GET  /api/mca/providers                         -> Relay provider registry
 *   - GET  /api/mca/providers/{id}/upload-readiness   -> authoritative send decision
 *   - GET  /api/attachments                           -> transfer archive
 *   - GET  /api/attachments/{id}                      -> one transfer + timeline
 *   - GET  /api/attachments/{id}/content              -> verified plaintext
 *   - GET  /api/mca/commands/{id}                     -> async command status
 *   - POST /api/attachments                           -> multipart create (202)
 *   - POST /api/attachments/{id}/download|reject|save|cancel|revoke|retry (202)
 *   - DELETE /api/attachments/{id}/local-content      (202)
 *   - POST /api/mca/contacts/{id}/request-key|confirm|key-change/accept|key-change/reject
 *   - POST /api/mca/providers/probe, POST /api/mca/providers
 *   - PATCH/DELETE /api/mca/providers/{id}, POST /api/mca/providers/{id}/default
 *   - PUT/DELETE /api/mca/providers/{id}/upload-token, POST /api/mca/providers/{id}/check
 *
 * Every mutating request is a plain `fetch` — static/csrf.js monkey-patches
 * window.fetch to add the CSRF header to same-origin mutating /api/ requests.
 * Dynamic identifiers are never embedded in inline event handlers: markup
 * carries `data-files-*` attributes and one delegated document-level listener
 * dispatches them against the current in-memory model.
 *
 * This is a self-contained module: it defines its own `esc()` (rather than
 * relying on chat.js's DOM-based `escapeHtml`) and only reaches for the
 * shared notification helpers (`showToast`/`showProgressNotification`/
 * `updateNotification`) and `window.I18N` at call time.
 */

'use strict';

(function () {
    // ---- constants ---------------------------------------------------------

    var POLL_ACTIVE_MS = 3000;      // while a transfer is non-terminal / a command is tracked
    var POLL_IDLE_MS = 15000;       // while visible and idle
    var COMMAND_POLL_MS = 600;      // command-status poll cadence (500..800ms window)
    var COMMAND_MAX_WAIT_MS = 60000;
    var LIST_LIMIT = 500;           // mirrors _LIST_LIMIT_MAX server-side
    var MAX_SEND_BYTES = 5 * 1024 * 1024; // mirrors _MAX_FILE_BYTES server-side
    var CONTACT_ID_RE = /^![0-9a-f]{8}$/;  // mirrors _CONTACT_ID_RE server-side

    // ---- backend state machines (mirrors meshsrv/attachments/{sender,receiver}.py) ----

    var SENDER_AUTOMATIC = ['DRAFT', 'VALIDATING', 'ENCRYPTING', 'QUEUED_UPLOAD',
        'UPLOADING', 'READY_TO_SEND'];
    var SENDER_REVOKABLE = ['SENT', 'RECEIVED', 'DOWNLOADED'];
    var RECEIVER_AUTOMATIC = ['WAITING_KEY', 'WAITING_PROVIDER', 'WAITING_NETWORK', 'DOWNLOADING'];

    var SENDER_TERMINAL = { DOWNLOADED: 1, EXPIRED: 1, REVOKED: 1, CANCELLED: 1,
        FAILED_VALIDATION: 1, FAILED_UPLOAD: 1, FAILED_RADIO: 1 };
    var RECEIVER_TERMINAL = { AVAILABLE: 1, EXPIRED: 1, REJECTED: 1, FAILED: 1 };

    // Attention states for the archive summary (C5 §10.3) — a transfer the
    // user is expected to act on. WAITING_NETWORK and success states are
    // deliberately excluded.
    var ATTENTION_STATES = { WAITING_CONSENT: 1, WAITING_KEY: 1, WAITING_PROVIDER: 1,
        FAILED_VALIDATION: 1, FAILED_UPLOAD: 1, FAILED_RADIO: 1, FAILED: 1 };

    // The six visible archive filters -> list-endpoint query (C5 §10.2).
    var FILTER_API = {
        all:      { direction: 'all', filter: 'all' },
        received: { direction: 'received', filter: 'all' },
        sent:     { direction: 'sent', filter: 'all' },
        pending:  { direction: 'all', filter: 'pending' },
        saved:    { direction: 'all', filter: 'saved' },
        errors:   { direction: 'all', filter: 'errors' },
    };

    var IMAGE_MIME = { 'image/jpeg': 1, 'image/png': 1, 'image/webp': 1 };
    var ALLOWED_MIME = { 'image/jpeg': 1, 'image/png': 1, 'image/webp': 1,
        'application/pdf': 1, 'text/plain': 1, 'text/csv': 1, 'application/json': 1 };
    // Extension -> MIME for client-side feedback only (server is authoritative).
    var EXTENSION_MIME = {
        jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png', webp: 'image/webp',
        pdf: 'application/pdf', txt: 'text/plain', log: 'text/plain',
        csv: 'text/csv', json: 'application/json',
    };

    // ---- fallback labels (English; i18n catalogs are the source of truth) ----

    var FILES_STATE_LABELS = {
        DRAFT: 'Draft',
        VALIDATING: 'Validating',
        ENCRYPTING: 'Encrypting',
        QUEUED_UPLOAD: 'Queued for upload',
        UPLOADING: 'Uploading',
        READY_TO_SEND: 'Ready to send',
        SENT: 'Sent',
        RECEIVED: 'Received',
        DOWNLOADED: 'Downloaded',
        EXPIRED: 'Expired',
        REVOKED: 'Revoked',
        CANCELLED: 'Cancelled',
        FAILED_VALIDATION: 'Validation failed',
        FAILED_UPLOAD: 'Upload failed',
        FAILED_RADIO: 'Radio send failed',
        OFFER_RECEIVED: 'Offer received',
        WAITING_KEY: 'Waiting for key',
        WAITING_PROVIDER: 'Waiting for provider',
        WAITING_NETWORK: 'Waiting for network',
        WAITING_CONSENT: 'Waiting for consent',
        DOWNLOADING: 'Downloading',
        VERIFYING: 'Verifying',
        AVAILABLE: 'Available',
        REJECTED: 'Rejected',
        FAILED: 'Failed',
    };

    var FILES_CONTACT_STATUS_LABELS = {
        trusted: 'Trusted',
        confirmation_required: 'Confirmation required',
        key_changed: 'Key changed',
        key_unknown: 'Key unknown',
    };

    var FILES_RELAY_STATE_LABELS = {
        unknown: 'Unknown',
        online: 'Online',
        degraded: 'Degraded',
        unreachable: 'Unreachable',
        identity_mismatch: 'Identity mismatch',
        incompatible: 'Incompatible',
        disabled: 'Disabled',
    };

    var FILES_READINESS_LABELS = {
        profile_not_found: 'Provider not found',
        profile_disabled: 'Provider is disabled',
        upload_not_allowed: 'Uploads are not allowed for this provider',
        upload_token_missing: 'No upload token configured',
        relay_not_yet_checked: 'Provider not yet checked',
        relay_unreachable: 'Provider is unreachable',
        relay_identity_mismatch: 'Provider identity mismatch',
        relay_incompatible: 'Provider is incompatible',
        ciphertext_too_large: 'File exceeds the provider size limit',
        ttl_below_minimum: 'Expiry is below the provider minimum',
        ttl_above_maximum: 'Expiry is above the provider maximum',
    };

    // ---- single internal state object --------------------------------------

    var state = {
        active: false,
        visible: true,
        epoch: 0,                 // bumped on activate()/deactivate() — stale reads are dropped
        refreshTimer: null,
        loading: {},              // per-resource in-flight guard (no overlap)
        busy: {},                 // resourceKey -> commandId currently tracked
        detailSeq: 0,             // guards stale detail renders (V2)
        detailInFlight: false,    // at most one detail fetch at a time (R4)
        detailPending: null,      // coalesced follow-up id, or null (R4)
        detailFingerprint: null,  // signature of the last-rendered detail (R4)

        contacts: [],             // merged contact projection (C3)
        nodes: [],                // raw /api/nodes_management nodes
        providers: [],
        connectivity: { internet: 'unknown', relays: {} },
        settings: null,
        localNodeId: '',

        attachments: [],
        total: 0,
        truncated: false,
        filter: 'all',
        search: '',
        selectedId: null,

        sendSignature: null,      // semantic send-form signature (C7 idempotency)
        sendClientRequestId: null,
        sendInFlight: false,

        dialog: null,             // currently-open dialog element
        dialogReturnFocus: null,  // element to restore focus to on close (C11)
        modalSeq: 0,              // id suffix for auto-wired aria-describedby (R6)
        providerProbe: null,      // last successful probe result (pending registration)
    };

    // ---- small helpers ------------------------------------------------------

    function esc(value) {
        if (value === null || value === undefined) return '';
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function t(key, fallback) {
        if (typeof window !== 'undefined' && window.I18N && typeof window.I18N.tOrFallback === 'function') {
            return window.I18N.tOrFallback(key, {}, fallback);
        }
        return fallback;
    }

    function tparams(key, params, fallback) {
        if (typeof window !== 'undefined' && window.I18N && typeof window.I18N.tOrFallback === 'function') {
            return window.I18N.tOrFallback(key, params, fallback);
        }
        return fallback;
    }

    function getEl(id) {
        if (typeof document === 'undefined') return null;
        return document.getElementById(id);
    }

    function filesStateLabel(s) { return t('files.state.' + s, FILES_STATE_LABELS[s] || s); }
    function filesContactStatusLabel(s) { return t('files.contact.' + s, FILES_CONTACT_STATUS_LABELS[s] || s); }
    function filesRelayStateLabel(s) { return t('files.relay_state.' + s, FILES_RELAY_STATE_LABELS[s] || s); }
    function filesReadinessLabel(s) { return t('files.ready_reason.' + s, FILES_READINESS_LABELS[s] || s || 'Unknown'); }

    function filesErrorCode(data) {
        var code = (data && data.error_code) ? data.error_code : 'internal_error';
        var fallback = (data && data.error) || code;
        return t('files.error.' + code, fallback);
    }

    function sleep(ms) {
        return new Promise(function (resolve) { setTimeout(resolve, ms); });
    }

    function newClientRequestId() {
        try {
            if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
                return crypto.randomUUID();
            }
        } catch (_) { /* ignore */ }
        return 'req-' + Date.now() + '-' + Math.floor(Math.random() * 0xffffffff).toString(16);
    }

    function api(url, options) {
        var opts = options || {};
        var resp;
        return (typeof fetch === 'function' ? fetch(url, opts) : Promise.reject(new Error('no fetch'))).then(
            function (r) { resp = r; return r.json().catch(function () { return null; }); },
            function (err) {
                return { status: 0, data: { ok: false, error: String((err && err.message) || err), error_code: 'network_error' } };
            }
        ).then(function (data) {
            return { status: resp ? resp.status : 0, data: data || { ok: false, error: 'HTTP error', error_code: 'http_error' } };
        });
    }

    function fmtBytes(n) {
        if (n === null || n === undefined) return '—';
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function fmtDate(epochSeconds) {
        if (!epochSeconds) return '—';
        var d = new Date(epochSeconds * 1000);
        return d.toLocaleString ? d.toLocaleString() : String(d);
    }

    function fmtRel(epochSeconds) {
        if (!epochSeconds) return '—';
        var now = Math.floor(Date.now() / 1000);
        var s = epochSeconds - now;
        if (s < 0) return t('files.expired', 'Expired');
        if (s < 3600) return Math.max(1, Math.round(s / 60)) + 'm';
        if (s < 86400) return Math.round(s / 3600) + 'h';
        return Math.round(s / 86400) + 'd';
    }

    function fmtGrace(seconds) {
        if (!seconds) return '—';
        if (seconds < 3600) return Math.round(seconds / 60) + 'm';
        if (seconds < 86400) return Math.round(seconds / 3600) + 'h';
        return Math.round(seconds / 86400) + 'd';
    }

    function groupFp(fp) {
        if (!fp) return '';
        return fp.match(/.{1,4}/g).join(' ');
    }

    function mimeIcon(mime) {
        if (!mime) return '📄';
        if (IMAGE_MIME[mime]) return '🖼️';
        if (mime === 'application/pdf') return '📕';
        return '📄';
    }

    function isTerminalState(s) {
        return Boolean(SENDER_TERMINAL[s] || RECEIVER_TERMINAL[s]);
    }

    function isAttention(a) {
        if (ATTENTION_STATES[a.state]) return true;
        return a.error_code === 'recipient_provider_unknown';
    }

    function hasActiveCommand() {
        return Object.keys(state.busy).length > 0;
    }

    function canonicalNodeId(id) {
        return String(id || '').toLowerCase();
    }

    function isLocalNode(id) {
        return state.localNodeId && canonicalNodeId(id) === canonicalNodeId(state.localNodeId);
    }

    // ---- notification helpers (delegate to the shared Notification Center) ----

    function toast(message, type) {
        if (typeof showToast === 'function') showToast(message, type || 'info');
    }

    function notifyProgress(message) {
        if (typeof showProgressNotification === 'function') return showProgressNotification(message);
        return null;
    }

    function notifyUpdate(id, message, type) {
        if (typeof updateNotification === 'function' && id !== null) {
            updateNotification(id, message, type || 'info');
        } else {
            toast(message, type);
        }
    }

    // ---- command tracker (C2) — one generic path for every 202 {command_id} ----

    function setBusy(resourceKey, commandId) {
        state.busy[resourceKey] = commandId;
        rerenderBusyControls(resourceKey);
    }

    function clearBusy(resourceKey) {
        delete state.busy[resourceKey];
        rerenderBusyControls(resourceKey);
    }

    function rerenderBusyControls() {
        // Cheap targeted re-render so a control flips to disabled the moment its
        // command is tracked, and back when it settles.
        if (state.dialog) {
            var dl = state.dialog;
            if (dl.className.indexOf('is-providers') !== -1) renderProviderSettings();
        }
        if (state.active && state.selectedId) renderDetail(state.selectedId, true);
        if (state.active) renderTransfers();
    }

    function trackCommand(commandId, opts) {
        var progressId = notifyProgress(opts.queued);
        setBusy(opts.resourceKey, commandId);

        (function poll() {
            var started = Date.now();
            function step() {
                if (Date.now() - started >= COMMAND_MAX_WAIT_MS) {
                    notifyUpdate(progressId, t('files.cmd_timeout', 'Timed out'), 'warning');
                    clearBusy(opts.resourceKey);
                    if (opts.onTimeout) opts.onTimeout();
                    return;
                }
                sleep(COMMAND_POLL_MS).then(function () {
                    return api('/api/mca/commands/' + encodeURIComponent(commandId));
                }).then(function (r) {
                    if (r.status === 404) {
                        // Unknown after a service restart: refresh the domain
                        // projection, never claim success, never replay.
                        Promise.resolve(opts.onUnknown ? opts.onUnknown() : null).then(function () {
                            notifyUpdate(progressId, t('files.cmd_unknown', 'Result unknown after restart'), 'warning');
                            clearBusy(opts.resourceKey);
                        });
                        return;
                    }
                    if (r.status === 200 && r.data && r.data.ok && r.data.command) {
                        var cmd = r.data.command;
                        if (cmd.status === 'succeeded') {
                            Promise.resolve(opts.onSuccess ? opts.onSuccess(cmd) : null).then(function () {
                                notifyUpdate(progressId, opts.success, 'success');
                                clearBusy(opts.resourceKey);
                            });
                            return;
                        }
                        if (cmd.status === 'failed') {
                            notifyUpdate(progressId, filesErrorCode(cmd), 'error');
                            clearBusy(opts.resourceKey);
                            if (opts.onFailed) opts.onFailed(cmd);
                            return;
                        }
                        // still pending — keep polling, no notification churn
                    }
                    step();
                }).catch(function () {
                    step();
                });
            }
            step();
        })();
    }

    // ---- resource loaders (C1 §6.3 concurrency: guarded, no overlap) -------

    function guardedLoad(key, fetcher, onDone) {
        if (state.loading[key]) return Promise.resolve();
        state.loading[key] = true;
        var epoch = state.epoch;
        return fetcher().then(function (result) {
            state.loading[key] = false;
            if (epoch !== state.epoch || !state.active) return;
            onDone(result);
        }).catch(function () {
            state.loading[key] = false;
        });
    }

    function refreshContacts() {
        guardedLoad('contacts', function () {
            return Promise.all([
                api('/api/nodes_management'),
                api('/api/mca/contacts'),
                api('/api/base_status'),
            ]);
        }, function (results) {
            var nodesRes = results[0], contactsRes = results[1], baseRes = results[2];
            state.nodes = (nodesRes.status === 200 && nodesRes.data && Array.isArray(nodesRes.data.nodes))
                ? nodesRes.data.nodes : [];
            var bindings = (contactsRes.status === 200 && contactsRes.data && Array.isArray(contactsRes.data.contacts))
                ? contactsRes.data.contacts : [];
            if (baseRes.status === 200 && baseRes.data) {
                state.localNodeId = canonicalNodeId(baseRes.data.node_id || '');
            }
            state.contacts = mergeContacts(state.nodes, bindings, state.localNodeId);
            renderContacts();
        });
    }

    function loadProviders() {
        return guardedLoad('providers', function () {
            return api('/api/mca/providers');
        }, function (r) {
            if (r.status === 200 && r.data && r.data.ok) {
                state.providers = Array.isArray(r.data.providers) ? r.data.providers : [];
            } else if (r.status === 503) {
                state.providers = [];
            } else {
                state.providers = [];
            }
            renderProviderSettings();
        });
    }

    function loadConnectivity() {
        guardedLoad('connectivity', function () {
            return api('/api/mca/connectivity');
        }, function (r) {
            if (r.status === 200 && r.data && r.data.ok) {
                state.connectivity = {
                    internet: r.data.internet || 'unknown',
                    relays: r.data.relays || {},
                };
            }
        });
    }

    function loadSettings() {
        guardedLoad('settings', function () {
            return api('/api/settings');
        }, function (r) {
            if (r.status === 200 && r.data && r.data.ok && r.data.settings) {
                state.settings = r.data.settings;
            }
        });
    }

    function loadTransfers() {
        var mapping = FILTER_API[state.filter] || FILTER_API.all;
        var url = '/api/attachments?direction=' + encodeURIComponent(mapping.direction) +
            '&filter=' + encodeURIComponent(mapping.filter) +
            '&limit=' + LIST_LIMIT;
        guardedLoad('transfers', function () {
            return api(url);
        }, function (r) {
            if (r.status !== 200 || !r.data || r.data.ok !== true) {
                if (r.status === 503) renderTransfersError(t('files.not_ready', 'MCAttach service is not ready'));
                else renderTransfersError(filesErrorCode(r.data));
                return;
            }
            state.attachments = Array.isArray(r.data.attachments) ? r.data.attachments : [];
            state.total = r.data.total || 0;
            state.truncated = state.total > LIST_LIMIT;
            renderTransfers();
            renderSummaries();
            // Preserve selection if it still exists, else auto-select first (C5 §10.5).
            if (state.selectedId && !state.attachments.some(function (a) { return a.id === state.selectedId; })) {
                state.selectedId = null;
            }
            if (!state.selectedId && state.attachments.length) {
                state.selectedId = state.attachments[0].id;
                renderDetail(state.selectedId, true);
            } else if (state.selectedId) {
                var sel = null;
                for (var i = 0; i < state.attachments.length; i++) {
                    if (state.attachments[i].id === state.selectedId) { sel = state.attachments[i]; break; }
                }
                // R4: a terminal attachment whose list projection is unchanged
                // since the last detail render does not need a per-poll detail
                // fetch; only active (still-changing) or changed attachments do.
                if (sel && (!isTerminalState(sel.state) || detailFingerprint(sel) !== state.detailFingerprint)) {
                    renderDetail(state.selectedId, true);
                }
            }
        });
    }

    // ---- contact merge (C3 §8.1) -------------------------------------------

    function mergeContacts(nodes, bindings, localNodeId) {
        var byId = {};
        var order = [];

        bindings.forEach(function (b) {
            var id = canonicalNodeId(b.contact_id);
            byId[id] = {
                contact_id: id,
                status: b.status,
                fingerprint: b.fingerprint || '',
                pending_fingerprint: b.pending_fingerprint || '',
                key_epoch: b.key_epoch,
                pending_key_epoch: b.pending_key_epoch,
                name: '',
                hasBinding: true,
            };
            if (order.indexOf(id) === -1) order.push(id);
        });

        nodes.forEach(function (n) {
            var id = canonicalNodeId(n.node_id);
            if (!id || isLocalNodeId(id, localNodeId)) return;
            if (!byId[id]) {
                byId[id] = {
                    contact_id: id,
                    status: 'key_unknown',
                    fingerprint: '',
                    pending_fingerprint: '',
                    key_epoch: null,
                    pending_key_epoch: null,
                    name: n.name || '',
                    hasBinding: false,
                };
                order.push(id);
            } else if (!byId[id].name) {
                byId[id].name = n.name || '';
            }
        });

        var merged = order.map(function (id) { return byId[id]; });
        merged.sort(function (a, b) {
            var an = (a.name || '').toLowerCase();
            var bn = (b.name || '').toLowerCase();
            if (an < bn) return -1;
            if (an > bn) return 1;
            return a.contact_id < b.contact_id ? -1 : a.contact_id > b.contact_id ? 1 : 0;
        });
        return merged;
    }

    function isLocalNodeId(id, localNodeId) {
        return Boolean(localNodeId && canonicalNodeId(id) === localNodeId);
    }

    function contactByNameOrId(c, contactId) {
        return canonicalNodeId(c.contact_id) === canonicalNodeId(contactId);
    }

    function findContact(contactId) {
        for (var i = 0; i < state.contacts.length; i++) {
            if (contactByNameOrId(state.contacts[i], contactId)) return state.contacts[i];
        }
        return null;
    }

    // ---- rendering: contacts (C3) ------------------------------------------

    function renderContacts() {
        var list = getEl('filesContactsList');
        if (!list) return;
        if (!state.contacts.length) {
            list.innerHTML = '<div class="files-empty">' + esc(t('files.no_contacts', 'No known contacts yet.')) + '</div>';
            return;
        }
        var rows = state.contacts.map(function (c) {
            var status = filesContactStatusLabel(c.status);
            var shortFp = c.fingerprint ? c.fingerprint.slice(0, 16) : '';
            var trust = contactTrustActions(c);
            var name = c.name
                ? '<span class="files-contact-name">' + esc(c.name) + '</span>'
                : '';
            return (
                '<div class="files-contact-item' + (c.status === 'trusted' ? ' is-trusted' : '') + '" data-contact="' + esc(c.contact_id) + '">' +
                    '<div class="files-contact-head">' +
                        name +
                        '<span class="files-contact-id">' + esc(c.contact_id) + '</span>' +
                        '<span class="files-contact-status files-status-' + esc(c.status) + '">' + esc(status) + '</span>' +
                    '</div>' +
                    '<div class="files-contact-fp">' + esc(shortFp) + '</div>' +
                    trust +
                '</div>'
            );
        }).join('');
        list.innerHTML = rows;
    }

    function contactTrustActions(c) {
        var actions = '';
        if (c.status === 'confirmation_required') {
            actions += '<button type="button" class="files-action-btn" data-files-action="contact-confirm" data-contact="' + esc(c.contact_id) + '">' +
                esc(t('files.trust_confirm', 'Trust key')) + '</button>';
        }
        if (c.status === 'key_changed') {
            actions += '<button type="button" class="files-action-btn" data-files-action="contact-accept" data-contact="' + esc(c.contact_id) + '">' +
                esc(t('files.key_change_accept', 'Accept')) + '</button>';
            actions += '<button type="button" class="files-action-btn is-danger" data-files-action="contact-reject" data-contact="' + esc(c.contact_id) + '">' +
                esc(t('files.key_change_reject', 'Reject')) + '</button>';
        }
        if (c.status === 'key_unknown') {
            actions += '<button type="button" class="files-action-btn" data-files-action="contact-request-key" data-contact="' + esc(c.contact_id) + '">' +
                esc(t('files.request_key', 'Request key')) + '</button>';
        }
        if (actions) return '<div class="files-contact-actions">' + actions + '</div>';
        return '';
    }

    function renderContactsError(message) {
        var list = getEl('filesContactsList');
        if (!list) return;
        state.contacts = [];
        list.innerHTML = '<div class="files-empty">' + esc(message) + '</div>';
    }

    // ---- rendering: archive (C5) -------------------------------------------

    function renderTransfersError(message) {
        var list = getEl('filesArchiveList');
        if (!list) return;
        list.innerHTML = '<div class="files-empty">' + esc(message) + '</div>';
    }

    function visibleAttachments() {
        var q = state.search.trim().toLowerCase();
        if (!q) return state.attachments;
        return state.attachments.filter(function (a) {
            var provider = providerById(a.provider_id);
            var contact = contactForAttachment(a);
            var hay = [
                a.file_name || '',
                (contact && contact.name) || '',
                (contact && contact.contact_id) || '',
                (provider && provider.display_name) || '',
                (provider && provider.origin) || '',
                filesStateLabel(a.state),
            ].join(' ').toLowerCase();
            return hay.indexOf(q) !== -1;
        });
    }

    function contactForAttachment(a) {
        // §7.5 counterparty_contact_id: a canonical transport address the
        // worker derives from routing data (sent DIRECT delivery route_id /
        // received DIRECT reply_route_id), never from recipient.principal_id
        // (a different 16-hex namespace). When it is null (missing / ambiguous
        // / non-DIRECT) there is no trustworthy contact mapping, so return
        // null rather than guessing from a wrong-namespace recipient field.
        if (!a.counterparty_contact_id) return null;
        return findContact(a.counterparty_contact_id);
    }

    function providerById(pid) {
        for (var i = 0; i < state.providers.length; i++) {
            if (state.providers[i].provider_id === pid) return state.providers[i];
        }
        return null;
    }

    function renderTransfers() {
        var list = getEl('filesArchiveList');
        if (!list) return;
        var items = visibleAttachments();
        if (!items.length) {
            list.innerHTML = '<div class="files-empty">' +
                esc(t('files.no_transfers', 'No transfers match this filter.')) + '</div>';
            return;
        }
        list.innerHTML = items.map(function (a) {
            var provider = providerById(a.provider_id);
            var contact = contactForAttachment(a);
            var dir = a.direction === 'sent' ? '↑' : '↓';
            var dirLabel = a.direction === 'sent'
                ? t('files.sent', 'Sent') : t('files.received', 'Received');
            var selected = a.id === state.selectedId ? ' is-selected' : '';
            var pressed = a.id === state.selectedId ? 'true' : 'false';
            var active = !isTerminalState(a.state) ? ' is-active' : '';
            var contactLabel = contact
                ? (esc(contact.name || '') + ' <span class="files-transfer-id">' + esc(contact.contact_id) + '</span>')
                : '';
            var providerLabel = provider ? esc(provider.display_name || provider.origin || '') : '';
            var expiry = a.hard_expires_at
                ? '<span class="files-transfer-expiry" title="' + esc(fmtDate(a.hard_expires_at)) + '">' + esc(fmtRel(a.hard_expires_at)) + '</span>'
                : '';
            var busy = state.busy['attach:' + a.id] ? ' is-busy' : '';
            return (
                '<button type="button" class="files-transfer-item' + selected + active + busy + '" aria-pressed="' + pressed + '" data-files-action="select" data-attachment="' + esc(a.id) + '">' +
                    '<span class="files-transfer-icon">' + mimeIcon(a.mime_type) + '</span>' +
                    '<span class="files-transfer-dir" title="' + esc(dirLabel) + '">' + dir + '</span>' +
                    '<span class="files-transfer-main">' +
                        '<span class="files-transfer-name">' + esc(a.file_name || t('files.encrypted_file', 'Encrypted file')) + '</span>' +
                        '<span class="files-transfer-sub">' + contactLabel + '</span>' +
                    '</span>' +
                    '<span class="files-transfer-provider">' + providerLabel + '</span>' +
                    '<span class="files-transfer-state files-state-' + esc(a.state) + '">' + esc(filesStateLabel(a.state)) + '</span>' +
                    '<span class="files-transfer-size">' + esc(fmtBytes(a.plain_size)) + '</span>' +
                    '<span class="files-transfer-date">' + esc(fmtDate(a.created_at)) + '</span>' +
                    expiry +
                '</button>'
            );
        }).join('');
    }

    function renderSummaries() {
        var summary = getEl('filesSummary');
        if (!summary) return;
        var attention = state.attachments.filter(isAttention).length;
        var savedBytes = state.attachments.reduce(function (acc, a) {
            return acc + (a.saved ? (a.plain_size || 0) : 0);
        }, 0);

        var parts = [];
        if (attention > 0) {
            parts.push('<span class="files-summary-attention">' +
                esc(tparams('files.attention_count', { count: attention }, attention + ' need attention')) + '</span>');
        }
        if (savedBytes > 0 && state.truncated) {
            parts.push('<span class="files-summary-saved">' +
                esc(tparams('files.saved_bytes_subset', { bytes: fmtBytes(savedBytes) }, fmtBytes(savedBytes) + ' saved')) + '</span>');
        } else if (savedBytes > 0) {
            parts.push('<span class="files-summary-saved">' +
                esc(tparams('files.saved_bytes', { bytes: fmtBytes(savedBytes) }, fmtBytes(savedBytes) + ' saved')) + '</span>');
        }
        if (state.truncated) {
            parts.push('<span class="files-summary-truncated">' +
                esc(tparams('files.truncated_notice', { limit: LIST_LIMIT, total: state.total },
                    'Showing the newest ' + LIST_LIMIT + ' of ' + state.total + ' transfers')) + '</span>');
        }
        summary.innerHTML = parts.join('');
    }

    // ---- rendering: detail + action matrix (C4/C6) -------------------------

    function selectAttachment(id) {
        state.selectedId = id;
        state.detailSeq++;
        renderTransfers();
        renderDetail(id, false);
    }

    function detailFingerprint(a) {
        // A cheap signature of everything detailMarkup shows that can change
        // without the list projection changing. Built from the *list*
        // projection's own fields (id/state/error_code/saved/content_available/
        // delivery states), so it can be compared against the fingerprint
        // stored after the last *detail* fetch to decide whether a terminal
        // attachment's detail needs re-fetching at all (R4).
        var parts = [a.id, a.state, a.error_code || '', a.saved ? '1' : '0', a.content_available ? '1' : '0'];
        var deliveries = a.deliveries || [];
        for (var i = 0; i < deliveries.length; i++) parts.push(deliveries[i].state || '');
        return parts.join('\u0000');
    }

    function renderDetail(id, silent) {
        var body = getEl('filesDetailBody');
        if (!body) return;

        // In-flight guard + coalescing (R4): never issue a second concurrent
        // detail request. A follow-up requested while one is running is
        // collapsed into a single pending refetch, fired once the in-flight
        // request settles.
        if (state.detailInFlight) {
            state.detailPending = id;
            return;
        }

        var token = ++state.detailSeq;
        var epoch = state.epoch;
        state.detailInFlight = true;
        state.detailPending = null;

        function settle() {
            state.detailInFlight = false;
            var pending = state.detailPending;
            state.detailPending = null;
            if (pending !== null && state.active && epoch === state.epoch) {
                renderDetail(pending, true);
            }
        }

        api('/api/attachments/' + encodeURIComponent(id)).then(function (r) {
            if (token !== state.detailSeq || epoch !== state.epoch) { settle(); return; } // stale (V2)
            if (!state.active) { settle(); return; }
            if (r.status !== 200 || !r.data || r.data.ok !== true) {
                body.innerHTML = '<div class="files-detail-empty">' + esc(filesErrorCode(r.data)) + '</div>';
                settle();
                return;
            }
            body.innerHTML = detailMarkup(r.data.attachment, Array.isArray(r.data.timeline) ? r.data.timeline : []);
            state.detailFingerprint = detailFingerprint(r.data.attachment);
            settle();
        }).catch(function () {
            if (token !== state.detailSeq || epoch !== state.epoch) { settle(); return; }
            if (!state.active) { settle(); return; }
            body.innerHTML = '<div class="files-detail-empty">' + esc(t('files.error.network_error', 'Network error')) + '</div>';
            settle();
        });
    }

    function detailActions(a) {
        var actions = [];
        if (a.direction === 'sent') {
            if (SENDER_AUTOMATIC.indexOf(a.state) !== -1) {
                actions.push(actionBtn(a.id, 'attach-retry', t('files.retry', 'Retry'), false));
                actions.push(actionBtn(a.id, 'attach-cancel', t('files.cancel', 'Cancel'), true));
            } else if (SENDER_REVOKABLE.indexOf(a.state) !== -1) {
                actions.push(actionBtn(a.id, 'attach-revoke', t('files.revoke', 'Revoke'), true));
            }
            // terminal FAILED_* / EXPIRED / REVOKED / CANCELLED -> no unsupported Retry (C4 §9.1)
        } else {
            if (RECEIVER_AUTOMATIC.indexOf(a.state) !== -1) {
                actions.push(actionBtn(a.id, 'attach-retry', t('files.retry', 'Retry'), false));
            } else if (a.state === 'WAITING_CONSENT') {
                actions.push(actionBtn(a.id, 'attach-download', t('files.accept_download', 'Accept and download'), false));
                actions.push(actionBtn(a.id, 'attach-reject', t('files.reject', 'Reject'), true));
            } else if (a.state === 'AVAILABLE') {
                actions = actions.concat(contentActions(a));
            }
            // OFFER_RECEIVED / VERIFYING / EXPIRED / REJECTED / FAILED -> no mutation
        }
        return actions.join('');
    }

    function contentActions(a) {
        var actions = [];
        if (a.saved) {
            actions.push(actionBtn(a.id, 'attach-delete-local', t('files.delete_local', 'Delete local copy'), true));
        } else {
            actions.push(actionBtn(a.id, 'attach-save', t('files.save_to_files', 'Save to Files'), false));
        }
        if (a.content_available) {
            if (IMAGE_MIME[a.mime_type]) {
                actions.push(actionBtn(a.id, 'attach-open', t('files.open_preview', 'Open preview'), false));
            }
            actions.push(actionBtn(a.id, 'attach-download-device', t('files.download_to_device', 'Download to device'), false));
        }
        return actions;
    }

    function actionBtn(id, action, label, danger) {
        var busy = state.busy['attach:' + id];
        var disabled = busy ? ' disabled' : '';
        return '<button type="button" class="files-action-btn' + (danger ? ' is-danger' : '') + '"' +
            ' data-files-action="' + esc(action) + '" data-attachment="' + esc(id) + '"' + disabled + '>' +
            esc(label) + '</button>';
    }

    function detailMarkup(a, timeline) {
        var contact = contactForAttachment(a);
        var recipientId = contact ? contact.contact_id : (a.counterparty_contact_id || '');
        var provider = providerById(a.provider_id);
        var providerLabel = provider ? (provider.display_name || provider.origin || a.provider_id) : (a.provider_id || '—');
        var route = a.primary_delivery_id ? t('files.detail_direct', 'Direct') : t('files.detail_direct', 'Direct');
        var deliveries = (Array.isArray(a.deliveries) && a.deliveries.length) ? a.deliveries : [];

        var rows = [
            ['files.detail_direction', 'Direction', a.direction === 'sent' ? t('files.sent', 'Sent') : t('files.received', 'Received')],
            ['files.detail_state', 'State', filesStateLabel(a.state)],
            ['files.detail_file', 'File', a.file_name || '—'],
            ['files.detail_type', 'Type', a.mime_type || '—'],
            ['files.detail_size', 'Size', fmtBytes(a.plain_size)],
            ['files.detail_recipient', 'Contact', recipientId || '—'],
            ['files.detail_provider', 'Provider', providerLabel],
            ['files.detail_route', 'Route', route],
            ['files.detail_created', 'Created', fmtDate(a.created_at)],
            ['files.detail_expires', 'Expires', fmtDate(a.hard_expires_at) + ' (' + fmtRel(a.hard_expires_at) + ')'],
            ['files.detail_grace', 'Download grace', fmtGrace(a.download_grace_seconds)],
            ['files.detail_saved', 'Saved', a.saved ? t('files.yes', 'Yes') : t('files.no', 'No')],
            ['files.detail_content', 'Content available', a.content_available ? t('files.yes', 'Yes') : t('files.no', 'No')],
        ];
        if (a.error_code) {
            rows.push(['files.detail_error', 'Error', a.error_code]);
        }

        var techRows = [
            ['files.tech_id', 'ID', a.id],
            ['files.tech_cipher_size', 'Ciphertext size', fmtBytes(a.cipher_size)],
            ['files.tech_provider_id', 'Provider ID', a.provider_id || '—'],
            ['files.tech_delivery_id', 'Primary delivery', a.primary_delivery_id || '—'],
        ];

        var timelineMarkup = timeline.length
            ? '<div class="files-detail-timeline">' + timeline.map(function (e) {
                return '<div class="files-timeline-event"><span class="files-timeline-time">' +
                    esc(fmtDate(e.created_at || e.at)) + '</span>' +
                    '<span class="files-timeline-text">' + esc(e.event_type || '') + '</span></div>';
            }).join('') + '</div>'
            : '';

        var deliveriesMarkup = deliveries.length
            ? '<div class="files-detail-deliveries"><div class="files-detail-subtitle">' +
                esc(t('files.detail_deliveries', 'Deliveries')) + '</div>' +
                deliveries.map(function (d) {
                    return '<div class="files-delivery">' +
                        '<span class="files-delivery-state files-state-' + esc(d.state) + '">' + esc(d.state) + '</span>' +
                        '<span class="files-delivery-route">' + esc(d.route_type || '') + '</span>' +
                        '<span class="files-delivery-sent">' + esc(fmtDate(d.sent_at)) + '</span>' +
                    '</div>';
                }).join('') + '</div>'
            : '';

        return '<div class="files-detail-head">' +
                '<div class="files-detail-name">' + esc(a.file_name || a.id) + '</div>' +
                '<div class="files-detail-state files-state-' + esc(a.state) + '">' + esc(filesStateLabel(a.state)) + '</div>' +
            '</div>' +
            '<div class="files-detail-table">' +
                rows.map(function (r) {
                    return '<div class="files-detail-row"><span class="files-detail-label">' +
                        esc(t(r[0], r[1])) + '</span><span class="files-detail-value">' + esc(r[2]) + '</span></div>';
                }).join('') +
            '</div>' +
            '<div class="files-detail-actions">' + detailActions(a) + '</div>' +
            deliveriesMarkup +
            timelineMarkup +
            '<details class="files-detail-tech">' +
                '<summary>' + esc(t('files.technical_details', 'Technical details')) + '</summary>' +
                '<div class="files-detail-table">' + techRows.map(function (r) {
                    return '<div class="files-detail-row"><span class="files-detail-label">' +
                        esc(t(r[0], r[1])) + '</span><span class="files-detail-value">' + esc(r[2]) + '</span></div>';
                }).join('') + '</div>' +
            '</details>';
    }

    // ---- attachment lifecycle actions (C4/C6) ------------------------------

    function attachmentCommand(id, action, opts) {
        var resourceKey = 'attach:' + id;
        if (state.busy[resourceKey]) return; // prevent a second conflicting operation
        api('/api/attachments/' + encodeURIComponent(id) + '/' + action, { method: 'POST' }).then(function (r) {
            if (r.status === 202 && r.data && r.data.command_id) {
                trackCommand(r.data.command_id, {
                    resourceKey: resourceKey,
                    queued: opts.queued,
                    success: opts.success,
                    onSuccess: function () { loadTransfers(); },
                    onUnknown: function () { loadTransfers(); },
                });
            } else if (r.status === 409) {
                toast(t('files.state_changed', 'This transfer changed — refreshing'), 'info');
                loadTransfers();
            } else {
                toast(filesErrorCode(r.data), 'error');
            }
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    function attachmentDownload(id) {
        confirmDialog(confirmSpec('files.confirm_accept_title', 'files.confirm_accept_body', 'files.accept_download',
            'Accept incoming transfer?', 'Download the verified content from this transfer.')).then(function (yes) {
            if (!yes) return;
            attachmentCommand(id, 'download', {
                queued: t('files.download_started', 'Download started'),
                success: t('files.downloaded', 'Downloaded'),
            });
        });
    }
    function attachmentReject(id) {
        confirmDialog(confirmSpec('files.confirm_reject_title', 'files.confirm_reject_body', 'files.reject',
            'Reject incoming transfer?', 'This declines the incoming transfer.')).then(function (yes) {
            if (!yes) return;
            attachmentCommand(id, 'reject', {
                queued: t('files.rejecting', 'Rejecting…'),
                success: t('files.rejected', 'Transfer rejected'),
            });
        });
    }
    function attachmentSave(id) {
        attachmentCommand(id, 'save', {
            queued: t('files.saving', 'Saving to Files…'),
            success: t('files.saved', 'Saved to Files'),
        });
    }
    function attachmentCancel(id) {
        confirmDialog(confirmSpec('files.confirm_cancel_title', 'files.confirm_cancel_body', 'files.cancel',
            'Cancel transfer?', 'This stops an in-progress outgoing transfer.')).then(function (yes) {
            if (!yes) return;
            attachmentCommand(id, 'cancel', {
                queued: t('files.cancelling', 'Cancelling…'),
                success: t('files.cancelled', 'Transfer cancelled'),
            });
        });
    }
    function attachmentRevoke(id) {
        confirmDialog(confirmSpec('files.confirm_revoke_title', 'files.confirm_revoke_body', 'files.revoke',
            'Revoke transfer?', 'This deletes the Relay copy of this transfer.')).then(function (yes) {
            if (!yes) return;
            attachmentCommand(id, 'revoke', {
                queued: t('files.revoking', 'Revoking…'),
                success: t('files.revoked', 'Transfer revoked'),
            });
        });
    }
    function attachmentRetry(id) {
        attachmentCommand(id, 'retry', {
            queued: t('files.retrying', 'Retrying…'),
            success: t('files.retried', 'Retry started'),
        });
    }
    function attachmentDeleteLocal(id) {
        confirmDialog(confirmSpec('files.confirm_delete_local_title', 'files.confirm_delete_local_body', 'files.delete_local',
            'Delete local copy?', 'This removes the saved copy on the Pi but keeps the transfer history.')).then(function (yes) {
            if (!yes) return;
            var resourceKey = 'attach:' + id;
            if (state.busy[resourceKey]) return;
            api('/api/attachments/' + encodeURIComponent(id) + '/local-content', { method: 'DELETE' }).then(function (r) {
                if (r.status === 202 && r.data && r.data.command_id) {
                    trackCommand(r.data.command_id, {
                        resourceKey: resourceKey,
                        queued: t('files.deleting_local', 'Deleting local copy…'),
                        success: t('files.local_deleted', 'Local copy deleted'),
                        onSuccess: function () { loadTransfers(); },
                        onUnknown: function () { loadTransfers(); },
                    });
                } else {
                    toast(filesErrorCode(r.data), 'error');
                }
            }).catch(function () {
                toast(t('files.error.network_error', 'Network error'), 'error');
            });
        });
    }

    // "Download to device" — a same-origin link to /content with a download
    // attribute; must never call /save (C6). Keeps backend security headers.
    function attachmentDownloadToDevice(id) {
        if (typeof document === 'undefined') return;
        var a = document.createElement('a');
        a.href = '/api/attachments/' + encodeURIComponent(id) + '/content';
        a.setAttribute('download', '');
        if (typeof document.body !== 'undefined' && document.body && document.body.appendChild) {
            document.body.appendChild(a);
        }
        a.click();
        if (a.parentNode) a.parentNode.removeChild(a);
    }

    // "Open preview" — inline image preview in a new tab.
    function attachmentOpen(id) {
        if (typeof window !== 'undefined' && typeof window.open === 'function') {
            window.open('/api/attachments/' + encodeURIComponent(id) + '/content', '_blank', 'noopener');
        }
    }

    // ---- contact trust actions (C3) ----------------------------------------

    function contactCommand(contactId, action, opts) {
        var resourceKey = 'contact:' + contactId;
        if (state.busy[resourceKey]) return;
        api('/api/mca/contacts/' + encodeURIComponent(contactId) + '/' + action, { method: 'POST' }).then(function (r) {
            if (r.status === 202 && r.data && r.data.command_id) {
                trackCommand(r.data.command_id, {
                    resourceKey: resourceKey,
                    queued: opts.queued,
                    success: opts.success,
                    onSuccess: function () { refreshContacts(); },
                    onUnknown: function () { refreshContacts(); },
                });
            } else if (r.status === 429) {
                var retry = (r.data && r.data.retry_after_seconds) || 600;
                toast(tparams('files.rate_limited', { seconds: retry }, 'Rate-limited — try again in ' + retry + 's'), 'error');
            } else if (r.status === 409) {
                toast(t('files.state_changed', 'This contact changed — refreshing'), 'info');
                refreshContacts();
            } else {
                toast(filesErrorCode(r.data), 'error');
            }
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    function contactRequestKey(contactId) {
        var c = findContact(contactId);
        var name = c && c.name ? c.name : contactId;
        confirmDialog({
            title: t('files.request_key_title', 'Request MCA key?'),
            bodyText: tparams('files.request_key_body', { name: name }, 'Ask ' + name + ' for their MCA encryption key.'),
            confirmLabel: t('files.request_key', 'Request key'),
            danger: false,
        }).then(function (yes) {
            if (!yes) return;
            contactCommand(contactId, 'request-key', {
                queued: t('files.requesting_key', 'Requesting key…'),
                success: t('files.request_key_sent', 'Key request queued'),
            });
        });
    }

    function contactConfirm(contactId) {
        var c = findContact(contactId);
        var fp = c && c.fingerprint ? c.fingerprint : '';
        var name = c && c.name ? c.name : contactId;
        confirmDialog({
            title: t('files.confirm_trust_title', 'Confirm contact key'),
            bodyHtml: fingerprintBlock(fp) + esc(tparams('files.confirm_trust_body', { name: name },
                'Verify this fingerprint with ' + name + ', then trust their key.')),
            confirmLabel: t('files.trust_confirm', 'Trust key'),
            danger: false,
        }).then(function (yes) {
            if (!yes) return;
            contactCommand(contactId, 'confirm', {
                queued: t('files.confirming', 'Confirming key…'),
                success: t('files.trust_confirmed', 'Key trusted'),
            });
        });
    }

    function contactAcceptKeyChange(contactId) {
        var c = findContact(contactId);
        var cur = c && c.fingerprint ? c.fingerprint : '';
        var pending = c && c.pending_fingerprint ? c.pending_fingerprint : '';
        confirmDialog({
            title: t('files.key_change_accept_title', 'Accept key change?'),
            bodyHtml: fpPairBlock(cur, pending) + esc(t('files.key_change_accept_body',
                'The new key becomes current but stays unverified until you confirm it separately.')),
            confirmLabel: t('files.key_change_accept', 'Accept'),
            danger: true,
        }).then(function (yes) {
            if (!yes) return;
            contactCommand(contactId, 'key-change/accept', {
                queued: t('files.accepting_key_change', 'Accepting key change…'),
                success: t('files.key_change_accepted', 'Key change accepted — confirm the new key next'),
            });
        });
    }

    function contactRejectKeyChange(contactId) {
        confirmDialog({
            title: t('files.key_change_reject_title', 'Reject key change?'),
            bodyText: t('files.key_change_reject_body',
                'The previous trusted key is kept and the pending replacement is discarded.'),
            confirmLabel: t('files.key_change_reject', 'Reject'),
            danger: true,
        }).then(function (yes) {
            if (!yes) return;
            contactCommand(contactId, 'key-change/reject', {
                queued: t('files.rejecting_key_change', 'Rejecting key change…'),
                success: t('files.key_change_rejected', 'Key change dismissed'),
            });
        });
    }

    function fingerprintBlock(fp) {
        if (!fp) return '';
        return '<div class="files-fingerprint-block">' +
            '<div class="files-fingerprint-label">' + esc(t('files.fingerprint_full', 'Full fingerprint')) + '</div>' +
            '<code class="files-fingerprint-code">' + esc(groupFp(fp)) + '</code>' +
            '<button type="button" class="files-action-btn" data-files-action="copy-fingerprint" data-fingerprint="' + esc(fp) + '">' +
                esc(t('files.fingerprint_copy', 'Copy fingerprint')) + '</button>' +
        '</div>';
    }

    function fpPairBlock(current, pending) {
        return '<div class="files-fingerprint-block">' +
            '<div class="files-fingerprint-label">' + esc(t('files.fingerprint_current', 'Current key fingerprint')) + '</div>' +
            '<code class="files-fingerprint-code">' + esc(groupFp(current)) + '</code>' +
            '<div class="files-fingerprint-label">' + esc(t('files.fingerprint_pending', 'Pending key fingerprint')) + '</div>' +
            '<code class="files-fingerprint-code">' + esc(groupFp(pending)) + '</code>' +
        '</div>';
    }

    function copyFingerprint(fp) {
        function done(ok) {
            toast(ok ? t('files.fingerprint_copied', 'Fingerprint copied')
                : t('files.fingerprint_copy_failed', 'Copy failed'), ok ? 'success' : 'error');
        }
        if (typeof navigator !== 'undefined' && navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(fp).then(function () { done(true); }, function () { done(false); });
        } else {
            done(false);
        }
    }

    // ---- confirmations (C9) — one reusable accessible component -------------

    function confirmSpec(titleKey, bodyKey, confirmKey, titleFallback, bodyFallback) {
        return {
            title: t(titleKey, titleFallback),
            bodyText: t(bodyKey, bodyFallback),
            confirmLabel: t(confirmKey, titleFallback),
            danger: true,
        };
    }

    // Resolve a confirmation body to trusted HTML. `bodyText` is plain,
    // untrusted text and is always escaped; `bodyHtml` is markup assembled
    // *inside this module* from static structure plus esc()-escaped values
    // (e.g. the fingerprint block). Supplying both is a caller bug and fails
    // closed with an empty body rather than letting ambiguous HTML through.
    function confirmBody(spec) {
        var hasText = spec.bodyText !== undefined && spec.bodyText !== null;
        var hasHtml = spec.bodyHtml !== undefined && spec.bodyHtml !== null;
        if (hasText && hasHtml) {
            return ''; // ambiguous body source — fail closed
        }
        if (hasHtml) return spec.bodyHtml;
        if (hasText) return esc(spec.bodyText);
        return '';
    }

    function confirmDialog(spec) {
        return new Promise(function (resolve) {
            if (typeof document === 'undefined') { resolve(true); return; }
            closeModal();
            var id = 'files-confirm-' + Date.now();
            var body = confirmBody(spec);
            var html =
                '<div class="files-modal-backdrop" data-files-action="modal-backdrop-close" role="presentation">' +
                    '<div class="files-modal" role="dialog" aria-modal="true" aria-labelledby="' + id + '-title">' +
                        '<div class="files-modal-header">' +
                            '<h3 class="files-modal-title" id="' + id + '-title">' + esc(spec.title) + '</h3>' +
                            '<button type="button" class="files-modal-close" data-files-action="modal-close" aria-label="' + esc(t('files.dialog_close', 'Close dialog')) + '">×</button>' +
                        '</div>' +
                        '<div class="files-modal-body">' + body + '</div>' +
                        '<div class="files-modal-footer">' +
                            '<button type="button" class="files-modal-cancel" data-files-action="modal-cancel">' + esc(t('common.cancel', 'Cancel')) + '</button>' +
                            '<button type="button" class="files-modal-submit' + (spec.danger ? ' is-danger' : '') + '" data-files-action="modal-confirm">' + esc(spec.confirmLabel) + '</button>' +
                        '</div>' +
                    '</div>' +
                '</div>';
            var el = openModalHtml(html, function (confirmed) { resolve(confirmed); });
            if (el) el.dataset.confirmResult = 'pending';
        });
    }

    // ---- modals + accessibility (C11) --------------------------------------

    function openModalHtml(html, onResolve) {
        var el = document.createElement('div');
        el.className = 'files-dialog-root';
        el.innerHTML = html;
        el._filesOnResolve = onResolve;
        // Wire aria-describedby once, centrally (R6): the dialog is labelled by
        // its title (aria-labelledby set by each builder) and described by its
        // body. Doing it here means every dialog gets it without duplicating it.
        var dialog = el.querySelector ? el.querySelector('.files-modal') : null;
        var body = el.querySelector ? el.querySelector('.files-modal-body') : null;
        if (dialog && body && !dialog.getAttribute('aria-describedby')) {
            if (!body.id) body.id = 'files-modal-body-' + (++state.modalSeq);
            dialog.setAttribute('aria-describedby', body.id);
        }
        document.body.appendChild(el);
        state.dialog = el;
        state.dialogReturnFocus = (typeof document.activeElement !== 'undefined') ? document.activeElement : null;
        setBackgroundInert(el);
        focusFirst(el);
        return el;
    }

    function closeModal(result) {
        if (!state.dialog) return;
        var el = state.dialog;
        state.dialog = null;
        if (el._filesOnResolve) el._filesOnResolve(result);
        if (el.parentNode) el.parentNode.removeChild(el);
        restoreBackgroundInert();
        if (state.dialogReturnFocus && typeof state.dialogReturnFocus.focus === 'function') {
            try { state.dialogReturnFocus.focus(); } catch (_) { /* ignore */ }
        }
        state.dialogReturnFocus = null;
    }

    function focusFirst(root) {
        var focusable = root.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
        if (focusable.length) {
            try { focusable[0].focus(); } catch (_) { /* ignore */ }
        }
    }

    function trapFocus(dialog, e) {
        // Keep keyboard focus inside an open modal (R6): Tab/Shift-Tab wrap at
        // the ends rather than escaping into the aria-hidden background.
        var nodes = dialog.querySelectorAll
            ? dialog.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')
            : [];
        var focusable = [];
        for (var i = 0; i < nodes.length; i++) {
            var n = nodes[i];
            if (n.disabled || n.getAttribute('aria-hidden') === 'true') continue;
            focusable.push(n);
        }
        if (!focusable.length) return;
        var first = focusable[0];
        var last = focusable[focusable.length - 1];
        var active = (typeof document !== 'undefined') ? document.activeElement : null;
        if (!active || !dialog.contains(active)) { e.preventDefault(); first.focus(); return; }
        if (e.shiftKey && active === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && active === last) { e.preventDefault(); first.focus(); }
    }

    // Background inertness (R6): while a modal is open, mark every element
    // sibling of the dialog root under <body> aria-hidden so a screen reader
    // cannot land on content behind the modal; the prior value is restored on
    // close. Uses the dialog root rather than a fixed container so it holds for
    // every dialog regardless of which workspace opened it.
    function setBackgroundInert(dialogRoot) {
        var body = (typeof document !== 'undefined') ? document.body : null;
        var children = body && (body.children || body._children) ? (body.children || body._children) : [];
        for (var i = 0; i < children.length; i++) {
            var c = children[i];
            if (c === dialogRoot) continue;
            if (typeof c._filesPrevAriaHidden === 'undefined') {
                c._filesPrevAriaHidden = c.getAttribute ? c.getAttribute('aria-hidden') : null;
            }
            if (c.setAttribute) c.setAttribute('aria-hidden', 'true');
        }
    }

    function restoreBackgroundInert() {
        var body = (typeof document !== 'undefined') ? document.body : null;
        var children = body && (body.children || body._children) ? (body.children || body._children) : [];
        for (var i = 0; i < children.length; i++) {
            var c = children[i];
            if (typeof c._filesPrevAriaHidden !== 'undefined') {
                if (c._filesPrevAriaHidden === null) {
                    if (c.removeAttribute) c.removeAttribute('aria-hidden');
                } else if (c.setAttribute) {
                    c.setAttribute('aria-hidden', c._filesPrevAriaHidden);
                }
                delete c._filesPrevAriaHidden;
            }
        }
    }

    function onDocumentKeydown(e) {
        if (!state.dialog) return;
        if (e.key === 'Escape') {
            // "Only when safe" (R6): never dismiss while a send request is in
            // flight, so an accidental Escape can't strand the send state machine.
            if (state.sendInFlight) return;
            closeModal(false);
            return;
        }
        if (e.key === 'Tab') trapFocus(state.dialog, e);
    }

    function onVisibilityChange() {
        var hidden = (typeof document !== 'undefined') && document.hidden;
        if (hidden) {
            state.visible = false;
            stopTimer();
        } else {
            state.visible = true;
            if (state.active) {
                refresh();
                schedule();
            }
        }
    }

    // ---- event delegation (C10) — one document-level click listener --------

    function closestAttr(el, attr) {
        var node = el;
        while (node && typeof node.getAttribute === 'function') {
            if (node.getAttribute && node.getAttribute(attr) !== null && node.getAttribute(attr) !== undefined) return node;
            node = node.parentNode;
        }
        return null;
    }

    function onDocumentClick(e) {
        var target = e.target || e.srcElement;
        if (!target) return;

        // Filter tabs (archive filters).
        var filterTab = closestAttr(target, 'data-files-filter');
        if (filterTab) { setFilter(filterTab.getAttribute('data-files-filter')); return; }

        // Modal backdrops / cancel / close / confirm.
        var modal = closestAttr(target, 'data-files-action');
        if (!modal) return;
        var action = modal.getAttribute('data-files-action');

        if (action === 'modal-backdrop-close') {
            if (target === modal) closeModal(false);
            return;
        }
        if (action === 'modal-close') { closeModal(false); return; }
        if (action === 'modal-cancel') { closeModal(false); return; }
        if (action === 'modal-confirm') { closeModal(true); return; }
        if (action === 'copy-fingerprint') { copyFingerprint(modal.getAttribute('data-fingerprint')); return; }

        // Workspace header actions.
        if (action === 'send') { openSendDialog(); return; }
        if (action === 'providers') { openProviderSettings(); return; }
        if (action === 'refresh') { refresh(); return; }

        // Attachment + contact + provider actions carry a data-attachment /
        // data-contact / data-provider id validated against the current model.
        var attachmentId = modal.getAttribute('data-attachment');
        var contactId = modal.getAttribute('data-contact');
        var providerId = modal.getAttribute('data-provider');

        if (attachmentId && state.attachments.some(function (a) { return a.id === attachmentId; })) {
            if (action === 'select') { selectAttachment(attachmentId); return; }
            if (action === 'attach-download') { attachmentDownload(attachmentId); return; }
            if (action === 'attach-reject') { attachmentReject(attachmentId); return; }
            if (action === 'attach-save') { attachmentSave(attachmentId); return; }
            if (action === 'attach-cancel') { attachmentCancel(attachmentId); return; }
            if (action === 'attach-revoke') { attachmentRevoke(attachmentId); return; }
            if (action === 'attach-retry') { attachmentRetry(attachmentId); return; }
            if (action === 'attach-delete-local') { attachmentDeleteLocal(attachmentId); return; }
            if (action === 'attach-open') { attachmentOpen(attachmentId); return; }
            if (action === 'attach-download-device') { attachmentDownloadToDevice(attachmentId); return; }
        }

        if (contactId && findContact(contactId)) {
            if (action === 'contact-request-key') { contactRequestKey(contactId); return; }
            if (action === 'contact-confirm') { contactConfirm(contactId); return; }
            if (action === 'contact-accept') { contactAcceptKeyChange(contactId); return; }
            if (action === 'contact-reject') { contactRejectKeyChange(contactId); return; }
        }

        if (providerId && providerById(providerId)) {
            if (action === 'provider-set-default') { providerSetDefault(providerId); return; }
            if (action === 'provider-toggle') { providerToggle(providerId); return; }
            if (action === 'provider-check') { providerCheck(providerId); return; }
            if (action === 'provider-edit') { openProviderEdit(providerId); return; }
            if (action === 'provider-remove') { providerRemove(providerId); return; }
            if (action === 'provider-save-token') { providerSetToken(providerId); return; }
            if (action === 'provider-clear-token') { providerClearToken(providerId); return; }
        }

        if (action === 'provider-save') { providerSave(); return; }
        if (action === 'provider-probe') { providerProbe(); return; }
        if (action === 'provider-register') { providerRegister(); return; }
        if (action === 'send-submit') { submitSend(); return; }
        if (action === 'send-cancel') { closeModal(false); return; }
    }

    function onDocumentInput(e) {
        var target = e.target;
        if (!target || !target.id) return;
        if (target.id === 'filesSearch') {
            state.search = target.value || '';
            renderTransfers();
            renderSummaries();
            return;
        }
        // The custom-TTL number input fires 'input' as digits are typed, so the
        // expiry summary updates live rather than only on blur.
        if (target.id === 'filesSendCustomTtlSeconds') {
            renderSendExpiry();
            return;
        }
    }

    // 'change' fires for <select> and <input type=file>, which do not emit a
    // useful 'input' event; this routes the send form's dynamic feedback.
    function onDocumentChange(e) {
        var target = e.target;
        if (!target || !target.id) return;
        if (target.id === 'filesSendExpiry') { renderSendCustomTtl(); return; }
        if (target.id === 'filesSendFile') { renderSendFileFeedback(); return; }
        if (target.id === 'filesSendProvider') { renderSendProviderReadiness(); return; }
    }

    // ---- filter (C5) -------------------------------------------------------

    function setFilter(filter) {
        if (!FILTER_API[filter]) return;
        state.filter = filter;
        var tabs = (typeof document !== 'undefined') ? document.querySelectorAll('#filesFilterTabs [data-files-filter]') : [];
        tabs.forEach ? tabs.forEach(function (btn) {
            var isActive = btn.getAttribute('data-files-filter') === filter;
            btn.classList.toggle('active', isActive);
            if (btn.setAttribute) btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
        }) : null;
        loadTransfers();
    }

    // ---- polling lifecycle (C1) --------------------------------------------

    function cadenceMs() {
        var anyActive = state.attachments.some(function (a) { return !isTerminalState(a.state); });
        if (anyActive || hasActiveCommand()) return POLL_ACTIVE_MS;
        return POLL_IDLE_MS;
    }

    function stopTimer() {
        if (state.refreshTimer !== null) {
            clearTimeout(state.refreshTimer);
            state.refreshTimer = null;
        }
    }

    function schedule() {
        if (!state.active || !state.visible) return;
        if (state.refreshTimer !== null) return;
        state.refreshTimer = setTimeout(function () {
            state.refreshTimer = null;
            tick();
        }, cadenceMs());
    }

    function tick() {
        if (!state.active || !state.visible) return;
        refresh();
        schedule();
    }

    function activate() {
        state.epoch++;
        state.active = true;
        state.visible = !(typeof document !== 'undefined' && document.hidden);
        refresh();
        schedule();
        loadConnectivity();
        loadSettings();
    }

    function deactivate() {
        state.active = false;
        state.epoch++;
        stopTimer();
        closeModal(false);
        // In-flight reads are dropped by the epoch bump; submitted commands are left untouched.
    }

    function refresh() {
        if (!state.active) return;
        refreshContacts();
        loadTransfers();
    }

    // ---- send dialog (C7) --------------------------------------------------

    function openSendDialog() {
        if (typeof document === 'undefined') return;
        closeModal();
        loadConnectivity();
        loadSettings();
        loadProvidersForSend();
        var id = 'files-send';
        var html =
            '<div class="files-modal-backdrop" data-files-action="modal-backdrop-close" role="presentation">' +
                '<div class="files-modal" role="dialog" aria-modal="true" aria-labelledby="' + id + '-title">' +
                    '<div class="files-modal-header">' +
                        '<h3 class="files-modal-title" id="' + id + '-title">📤 ' + esc(t('files.send_file', 'Send file')) + '</h3>' +
                        '<button type="button" class="files-modal-close" data-files-action="modal-close" aria-label="' + esc(t('files.dialog_close', 'Close dialog')) + '">×</button>' +
                    '</div>' +
                    '<div class="files-modal-body">' +
                        '<div class="files-send-status" id="filesSendStatus" aria-live="polite"></div>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_recipient', 'Recipient')) + '</span>' +
                            '<select id="filesSendRecipient"></select>' +
                        '</label>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_provider', 'Relay provider')) + '</span>' +
                            '<select id="filesSendProvider"></select>' +
                            '<div class="files-send-readiness" id="filesSendReadiness" aria-live="polite"></div>' +
                        '</label>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_file_label', 'File')) + '</span>' +
                            '<input type="file" id="filesSendFile" />' +
                            '<div class="files-send-file-feedback" id="filesSendFileFeedback" aria-live="polite"></div>' +
                        '</label>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_expiry', 'Expiry')) + '</span>' +
                            '<select id="filesSendExpiry">' +
                                '<option value="default">' + esc(t('files.expiry_default', 'Default (3 days)')) + '</option>' +
                                '<option value="extended">' + esc(t('files.expiry_extended', 'Extended (7 days)')) + '</option>' +
                                '<option value="custom">' + esc(t('files.expiry_custom', 'Custom')) + '</option>' +
                            '</select>' +
                            '<input type="number" id="filesSendCustomTtlSeconds" min="1" step="1" inputmode="numeric" placeholder="' +
                                esc(t('files.send_ttl_custom_placeholder', 'Seconds')) + '" autocomplete="off" style="display:none" />' +
                        '</label>' +
                        '<div class="files-send-expiry-summary" id="filesSendExpirySummary" aria-live="polite"></div>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_comment', 'Comment (optional)')) + '</span>' +
                            '<input type="text" id="filesSendComment" maxlength="280" autocomplete="off" />' +
                        '</label>' +
                    '</div>' +
                    '<div class="files-modal-footer">' +
                        '<button type="button" class="files-modal-cancel" data-files-action="send-cancel">' + esc(t('common.cancel', 'Cancel')) + '</button>' +
                        '<button type="button" class="files-modal-submit" data-files-action="send-submit" id="filesSendSubmit">' + esc(t('files.send_submit', 'Send')) + '</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
        var el = openModalHtml(html, function () { state.sendInFlight = false; });
        el.className = 'files-dialog-root is-send';
        renderSendRecipients();
        renderSendProviders();
        renderSendStatus();
        renderSendCustomTtl();
    }

    function loadProvidersForSend() {
        // R7: always fetch a FRESH provider projection on open — never trust
        // the cached list — so the Relay-state/readiness feedback reflects the
        // latest connectivity snapshot rather than one from an earlier visit.
        return guardedLoad('providers', function () { return api('/api/mca/providers'); }, function (r) {
            state.providers = (r.status === 200 && r.data && r.data.ok && Array.isArray(r.data.providers))
                ? r.data.providers : [];
            renderSendProviders();
            renderSendStatus();
        });
    }

    function sendTrustedContacts() {
        return state.contacts.filter(function (c) { return c.status === 'trusted'; });
    }

    function renderSendRecipients() {
        var sel = getEl('filesSendRecipient');
        if (!sel) return;
        if (!state.contacts.length) {
            sel.innerHTML = '<option value="">' + esc(t('files.no_contacts', 'No known contacts yet.')) + '</option>';
            return;
        }
        var trusted = state.contacts.filter(function (c) { return c.status === 'trusted'; });
        var options = state.contacts.map(function (c) {
            var name = c.name || c.contact_id;
            var shortFp = c.fingerprint ? c.fingerprint.slice(0, 8) : '';
            var label = name + ' (' + c.contact_id + ')';
            if (c.status === 'trusted') {
                return '<option value="' + esc(c.contact_id) + '">' + esc(label) + '</option>';
            }
            var reason = filesContactStatusLabel(c.status);
            return '<option value="' + esc(c.contact_id) + '" disabled>' + esc(label + ' — ' + reason) + '</option>';
        }).join('');
        if (!trusted.length) {
            options = '<option value="">' + esc(t('files.no_trusted_contacts', 'No trusted contacts')) + '</option>' + options;
        }
        sel.innerHTML = options;
    }

    function uploadReadyProvider(p) {
        return Boolean(p && p.enabled && p.upload_allowed && p.upload_token_configured &&
            p.upload_readiness === 'ready');
    }

    // Why a provider is not selectable for upload, as a short human label.
    // The list endpoint's `upload_readiness` is the config-only 3-value enum
    // (ready/upload_token_missing/upload_disabled), so the richer reasons are
    // derived from the profile flags directly; `upload_disabled` never reaches
    // `filesReadinessLabel` here (it maps to `upload_not_allowed` above).
    function providerNotReadyLabel(p) {
        if (!p) return filesReadinessLabel('profile_not_found');
        if (!p.enabled) return filesReadinessLabel('profile_disabled');
        if (!p.upload_allowed) return filesReadinessLabel('upload_not_allowed');
        if (!p.upload_token_configured) return filesReadinessLabel('upload_token_missing');
        return filesReadinessLabel(p.upload_readiness);
    }

    function renderSendProviders() {
        var sel = getEl('filesSendProvider');
        if (!sel) return;
        var prev = sel.value || '';

        if (!state.providers.length) {
            sel.innerHTML = '<option value="">' + esc(t('files.no_providers', 'No upload providers configured')) + '</option>';
            renderSendProviderReadiness();
            return;
        }

        // R7: list ALL providers, not just the ready ones — a non-ready
        // provider is shown (disabled) with the reason, so it never silently
        // disappears and the user can see *why* it is not selectable.
        sel.innerHTML = state.providers.map(function (p) {
            var label = p.display_name || p.origin || p.provider_id;
            if (uploadReadyProvider(p)) {
                return '<option value="' + esc(p.provider_id) + '">' + esc(label) + '</option>';
            }
            return '<option value="' + esc(p.provider_id) + '" disabled>' +
                esc(label + ' — ' + providerNotReadyLabel(p)) + '</option>';
        }).join('');

        // R7: no auto-switch. Preserve an existing selection when it is still a
        // ready option; only fall back to the default (then first ready) on
        // first render, when the user has not made a choice yet.
        if (prev && uploadReadyProvider(providerById(prev))) {
            sel.value = prev;
        } else if (!prev) {
            var chosen = null;
            for (var i = 0; i < state.providers.length; i++) {
                if (state.providers[i].is_default && uploadReadyProvider(state.providers[i])) { chosen = state.providers[i]; break; }
            }
            if (!chosen) {
                for (var j = 0; j < state.providers.length; j++) {
                    if (uploadReadyProvider(state.providers[j])) { chosen = state.providers[j]; break; }
                }
            }
            if (chosen) sel.value = chosen.provider_id;
        }
        renderSendProviderReadiness();
    }

    function renderSendStatus() {
        var status = getEl('filesSendStatus');
        if (!status) return;
        var parts = [];
        var internet = state.connectivity.internet;
        parts.push(esc(internet === 'online'
            ? t('files.internet_online', 'Internet: online')
            : t('files.internet_offline', 'Internet: offline')));
        if (state.settings && state.settings.meshtastic) {
            var transport = state.settings.meshtastic.transport;
            if (transport === 'bluetooth') {
                parts.push('<span class="files-status-warn">' + esc(t('files.transport_bluetooth', 'Transport: Bluetooth')) + '</span>');
            } else {
                parts.push(esc(t('files.transport_serial', 'Transport: USB serial')));
            }
        }
        status.innerHTML = parts.join(' · ');

        var submit = getEl('filesSendSubmit');
        var bluetooth = state.settings && state.settings.meshtastic && state.settings.meshtastic.transport === 'bluetooth';
        var offline = internet !== 'online';
        var label;
        if (bluetooth) {
            label = t('files.send_without_confirm', 'Send without confirmation');
        } else if (offline) {
            label = t('files.queue_submit', 'Queue');
        } else {
            label = t('files.send_submit', 'Send');
        }
        if (submit) submit.textContent = label;
    }

    function renderSendProviderReadiness() {
        var el = getEl('filesSendReadiness');
        if (!el) return;
        var sel = getEl('filesSendProvider');
        var id = sel ? sel.value : '';
        var p = id ? providerById(id) : null;
        if (!p) { el.innerHTML = ''; return; }
        // R7: Relay state and upload readiness are rendered as SEPARATE facts —
        // the relay's reachability/identity is independent of whether uploads
        // are configured/allowed for this profile.
        var relay = filesRelayStateLabel(p.state);
        var ready = uploadReadyProvider(p);
        var readiness = ready
            ? t('files.upload_ready', 'Upload: ready')
            : t('files.upload_not_ready', 'Upload: not ready') + ' — ' + providerNotReadyLabel(p);
        el.innerHTML =
            '<span>' + esc(t('files.relay_state_label', 'Relay state') + ': ' + relay) + '</span>' +
            ' · <span>' + esc(readiness) + '</span>';
    }

    function renderSendFileFeedback() {
        var el = getEl('filesSendFileFeedback');
        if (!el) return;
        var input = getEl('filesSendFile');
        var f = input && input.files && input.files.length ? input.files[0] : null;
        if (!f) { el.innerHTML = ''; return; }
        var name = (f.name || '').toLowerCase();
        var ext = name.indexOf('.') !== -1 ? name.split('.').pop() : '';
        // R7: advisory MIME feedback from the server-authoritative allowlist
        // (JPEG/PNG/WebP/PDF/TXT/LOG/CSV/JSON). The client only *describes* the
        // file here; the server sniffs magic bytes and is the real authority,
        // so an unrecognized type is a warning, never a hard block.
        var known = EXTENSION_MIME[ext] || (ALLOWED_MIME[f.type] ? f.type : '');
        var typeLine = known
            ? tparams('files.send_file_type', { type: known }, 'Type: ' + known)
            : t('files.send_file_type_unknown', 'Type: unrecognized — the server will validate it');
        el.innerHTML =
            '<span>' + esc(typeLine) + '</span>' +
            ' · <span>' + esc(tparams('files.send_file_size', { size: fmtBytes(f.size) }, 'Size: ' + fmtBytes(f.size))) + '</span>';
    }

    function renderSendCustomTtl() {
        var sel = getEl('filesSendExpiry');
        var input = getEl('filesSendCustomTtlSeconds');
        var custom = sel && sel.value === 'custom';
        if (input) input.style.display = custom ? '' : 'none';
        renderSendExpiry();
    }

    function renderSendExpiry() {
        var summary = getEl('filesSendExpirySummary');
        if (!summary) return;
        var ttl = sendTtlSeconds();
        var grace = sendGraceSeconds(ttl);
        var expiry = Math.floor(Date.now() / 1000) + ttl;
        summary.innerHTML =
            '<span>' + esc(tparams('files.expiry_summary', { date: fmtDate(expiry) }, 'Expires ' + fmtDate(expiry))) + '</span>' +
            ' · <span>' + esc(tparams('files.grace_summary', { grace: fmtGrace(grace) }, 'Download grace: ' + fmtGrace(grace))) + '</span>';
    }

    function sendTtlSeconds() {
        var sel = getEl('filesSendExpiry');
        var mode = sel ? sel.value : 'default';
        if (mode === 'extended') return 604800;
        if (mode === 'custom') {
            var input = getEl('filesSendCustomTtlSeconds');
            var raw = input ? String(input.value || '').trim() : '';
            var n = parseInt(raw, 10);
            if (raw !== '' && Number.isFinite(n) && String(n) === raw && n > 0) return n;
            return 86400; // fallback; submitSend validates the custom value itself
        }
        return 259200; // default 3 days
    }

    function sendGraceSeconds(ttl) {
        return ttl === 604800 ? 86400 : 3600;
    }

    function sendSignature() {
        var recipient = getEl('filesSendRecipient');
        var provider = getEl('filesSendProvider');
        var file = getEl('filesSendFile');
        var expiry = getEl('filesSendExpiry');
        var comment = getEl('filesSendComment');
        var f = file && file.files && file.files.length ? file.files[0] : null;
        return [
            recipient ? recipient.value : '',
            provider ? provider.value : '',
            expiry ? expiry.value : 'default',
            String(sendTtlSeconds()), // custom TTL is part of the semantic form
            comment ? (comment.value || '').trim() : '',
            f ? f.name : '',
            f ? String(f.size) : '',
            f ? (f.type || '') : '',
            f ? String(f.lastModified || '') : '',
        ].join('\u0000');
    }

    // Re-arm the send form after any terminal outcome (success/timeout/failed/
    // unknown) or a pre-202 error, so the Send button never stays disabled while
    // the dialog is still open. Send-in-flight is a separate guard from the
    // button state, so a spurious re-enable cannot trigger a double submit.
    function unlockSend() {
        state.sendInFlight = false;
        var submit = getEl('filesSendSubmit');
        if (submit) submit.disabled = false;
    }

    function submitSend() {
        var recipient = getEl('filesSendRecipient');
        var provider = getEl('filesSendProvider');
        var fileInput = getEl('filesSendFile');
        var feedback = getEl('filesSendFileFeedback');

        if (state.sendInFlight) return; // JS in-flight guard, independent of button state

        if (!recipient || !recipient.value) {
            toast(t('files.err_no_recipient', 'Choose a trusted contact'), 'error');
            return;
        }
        if (!provider || !provider.value) {
            toast(t('files.err_no_provider', 'Choose a ready provider'), 'error');
            return;
        }
        if (!fileInput || !fileInput.files || !fileInput.files.length) {
            toast(t('files.err_no_file', 'Choose a file'), 'error');
            return;
        }
        var file = fileInput.files[0];
        if (file.size > MAX_SEND_BYTES) {
            if (feedback) feedback.textContent = t('files.err_file_too_large', 'File exceeds the 5 MiB cap');
            return;
        }
        if (!fileTypeAllowed(file)) {
            if (feedback) feedback.textContent = t('files.err_mime', 'File type is not allowed');
            return;
        }

        // R7: a custom expiry must be a positive whole number of seconds.
        if ((getEl('filesSendExpiry') || {}).value === 'custom') {
            var ttlInput = getEl('filesSendCustomTtlSeconds');
            var raw = ttlInput ? String(ttlInput.value || '').trim() : '';
            var n = parseInt(raw, 10);
            if (raw === '' || !Number.isFinite(n) || String(n) !== raw || n <= 0) {
                toast(t('files.err_ttl_custom', 'Enter a positive whole number of seconds for the custom expiry'), 'error');
                return;
            }
        }

        // Idempotency: reuse the client_request_id for an unchanged semantic
        // form; mint a new one on any semantic change (C7 §12.7).
        var sig = sendSignature();
        if (sig !== state.sendSignature || !state.sendClientRequestId) {
            state.sendSignature = sig;
            state.sendClientRequestId = newClientRequestId();
        }

        var ttl = sendTtlSeconds();
        var grace = sendGraceSeconds(ttl);
        var submit = getEl('filesSendSubmit');
        state.sendInFlight = true;
        if (submit) submit.disabled = true;

        // Authoritative readiness decision for the exact requested TTL.
        api('/api/mca/providers/' + encodeURIComponent(provider.value) +
            '/upload-readiness?requested_ttl_seconds=' + ttl).then(function (r) {
            if (!r.data || r.data.ok !== true || !r.data.ready) {
                unlockSend();
                var reason = r.data && r.data.reason ? filesReadinessLabel(r.data.reason) : t('files.err_provider_not_ready', 'Selected provider is not ready');
                toast(reason, 'error');
                return;
            }
            var metadata = {
                client_request_id: state.sendClientRequestId,
                recipient: { source_address: recipient.value },
                hard_ttl_seconds: ttl,
                download_grace_seconds: grace,
                comment: (getEl('filesSendComment') && getEl('filesSendComment').value.trim()) ? getEl('filesSendComment').value.trim() : null,
            };
            metadata.provider_id = provider.value;

            var form = new FormData();
            form.append('metadata', JSON.stringify(metadata));
            form.append('file', file);

            return api('/api/attachments', { method: 'POST', body: form }).then(function (r2) {
                if (r2.status === 202 && r2.data && r2.data.ok && r2.data.command_id) {
                    var attachmentId = r2.data.attachment_id || null;
                    trackCommand(r2.data.command_id, {
                        resourceKey: 'create',
                        queued: t('files.sending', 'Sending file…'),
                        success: t('files.send_queued', 'Transfer created'),
                        onSuccess: function () {
                            unlockSend();
                            state.sendSignature = null;
                            state.sendClientRequestId = null;
                            closeModal(false);
                            loadTransfers();
                            if (attachmentId) selectAttachment(attachmentId);
                        },
                        onFailed: function () {
                            // Keep the form (and its client_request_id) so the
                            // user can retry after correcting the error.
                            unlockSend();
                        },
                        onTimeout: function () {
                            // Same form -> same client_request_id on retry, so
                            // the server can dedupe the still-pending create.
                            unlockSend();
                        },
                        onUnknown: function () {
                            unlockSend();
                            loadTransfers();
                        },
                    });
                    // Do not claim success yet: the tracker owns the final outcome.
                } else {
                    unlockSend();
                    toast(filesErrorCode(r2.data), 'error');
                }
            });
        }).catch(function () {
            unlockSend();
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    function fileTypeAllowed(file) {
        var name = (file.name || '').toLowerCase();
        var ext = name.indexOf('.') !== -1 ? name.split('.').pop() : '';
        if (EXTENSION_MIME[ext] || ALLOWED_MIME[file.type]) {
            // extension or declared MIME is in the allowlist — good enough for feedback
            return true;
        }
        // Unknown extension and unknown MIME: let the server decide (authoritative).
        return true;
    }

    // ---- provider settings (C8) --------------------------------------------

    function openProviderSettings() {
        if (typeof document === 'undefined') return;
        closeModal();
        loadConnectivity();
        var id = 'files-providers';
        var html =
            '<div class="files-modal-backdrop" data-files-action="modal-backdrop-close" role="presentation">' +
                '<div class="files-modal files-modal-wide" role="dialog" aria-modal="true" aria-labelledby="' + id + '-title">' +
                    '<div class="files-modal-header">' +
                        '<h3 class="files-modal-title" id="' + id + '-title">⚙️ ' + esc(t('files.providers', 'Relay providers')) + '</h3>' +
                        '<button type="button" class="files-modal-close" data-files-action="modal-close" aria-label="' + esc(t('files.dialog_close', 'Close dialog')) + '">×</button>' +
                    '</div>' +
                    '<div class="files-modal-body">' +
                        '<div id="filesProvidersList"><div class="files-empty">' +
                            esc(t('files.loading_providers', 'Loading providers…')) +
                        '</div></div>' +
                        '<div class="files-provider-add">' +
                            '<h4 class="files-provider-add-title">' + esc(t('files.add_provider', 'Add provider')) + '</h4>' +
                            '<input type="url" id="filesProviderOrigin" placeholder="https://relay.example.com" autocomplete="off" />' +
                            '<button type="button" class="files-action-btn" data-files-action="provider-probe">' + esc(t('files.probe', 'Probe')) + '</button>' +
                            '<div id="filesProviderProbeResult"></div>' +
                        '</div>' +
                    '</div>' +
                    '<div class="files-modal-footer">' +
                        '<button type="button" class="files-modal-cancel" data-files-action="modal-cancel">' + esc(t('common.close', 'Close')) + '</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
        var el = openModalHtml(html, function () {});
        el.className = 'files-dialog-root is-providers';
        loadProviders();
    }

    function providerCommand(url, method, body, opts) {
        var options = { method: method };
        if (body !== undefined) {
            options.headers = { 'Content-Type': 'application/json' };
            options.body = JSON.stringify(body);
        }
        var resourceKey = opts.resourceKey || 'provider-command';
        if (state.busy[resourceKey]) return;
        api(url, options).then(function (r) {
            if (r.status === 202 && r.data && r.data.command_id) {
                trackCommand(r.data.command_id, {
                    resourceKey: resourceKey,
                    queued: opts.queued,
                    success: opts.success,
                    onSuccess: function () { return loadProviders(); },
                    onUnknown: function () { return loadProviders(); },
                });
            } else {
                toast(filesErrorCode(r.data), 'error');
            }
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    function providerSetDefault(id) {
        providerCommand('/api/mca/providers/' + encodeURIComponent(id) + '/default', 'POST', undefined, {
            resourceKey: 'provider:' + id,
            queued: t('files.setting_default', 'Setting default…'),
            success: t('files.default_set', 'Default provider updated'),
        });
    }
    function providerToggle(id) {
        var p = providerById(id);
        if (!p) return;
        providerCommand('/api/mca/providers/' + encodeURIComponent(id), 'PATCH', { enabled: !p.enabled }, {
            resourceKey: 'provider:' + id,
            queued: t('files.provider_updating', 'Updating provider…'),
            success: t('files.provider_updated', 'Provider updated'),
        });
    }
    function providerCheck(id) {
        providerCommand('/api/mca/providers/' + encodeURIComponent(id) + '/check', 'POST', undefined, {
            resourceKey: 'provider:' + id,
            queued: t('files.checking', 'Checking provider…'),
            success: t('files.check_started', 'Check started'),
        });
    }
    function providerRemove(id) {
        var p = providerById(id);
        var name = p ? (p.display_name || p.origin || id) : id;
        confirmDialog({
            title: t('files.confirm_provider_remove_title', 'Remove provider?'),
            bodyText: tparams('files.confirm_provider_remove_body', { name: name },
                'This removes or disables the Relay provider ' + name + '.'),
            confirmLabel: t('files.remove', 'Remove'),
            danger: true,
        }).then(function (yes) {
            if (!yes) return;
            providerCommand('/api/mca/providers/' + encodeURIComponent(id), 'DELETE', undefined, {
                resourceKey: 'provider:' + id,
                queued: t('files.removing_provider', 'Removing provider…'),
                success: t('files.provider_removed', 'Provider removed or disabled'),
            });
        });
    }
    function providerSetToken(id) {
        var input = getEl('filesToken-' + id);
        if (!input || !input.value.trim()) {
            toast(t('files.err_no_token', 'Enter a token'), 'error');
            return;
        }
        var token = input.value.trim();
        input.value = ''; // clear immediately, whether the command later succeeds or fails
        providerCommand('/api/mca/providers/' + encodeURIComponent(id) + '/upload-token', 'PUT', { upload_token: token }, {
            resourceKey: 'provider:' + id,
            queued: t('files.saving_token', 'Saving token…'),
            success: t('files.token_saved', 'Upload token saved'),
        });
    }
    function providerClearToken(id) {
        providerCommand('/api/mca/providers/' + encodeURIComponent(id) + '/upload-token', 'DELETE', undefined, {
            resourceKey: 'provider:' + id,
            queued: t('files.clearing_token', 'Clearing token…'),
            success: t('files.token_cleared', 'Upload token cleared'),
        });
    }

    // ---- provider editing (R5) ----------------------------------------------

    function providerEditFields(id) {
        var p = providerById(id);
        if (!p) return '';
        var minTtl = (p.min_ttl_seconds === null || p.min_ttl_seconds === undefined) ? '' : String(p.min_ttl_seconds);
        var maxTtl = (p.max_ttl_seconds === null || p.max_ttl_seconds === undefined) ? '' : String(p.max_ttl_seconds);
        var ttlPlaceholder = t('files.provider_ttl_unset', 'Unset');
        return (
            '<div class="files-edit-field">' +
                '<label for="filesEditName-' + esc(id) + '">' + esc(t('files.provider_display_name', 'Display name')) + '</label>' +
                '<input type="text" id="filesEditName-' + esc(id) + '" value="' + esc(p.display_name || '') + '" autocomplete="off" />' +
            '</div>' +
            '<div class="files-edit-field files-edit-field--check">' +
                '<label class="files-edit-check"><input type="checkbox" id="filesEditEnabled-' + esc(id) + '"' + (p.enabled ? ' checked' : '') + ' /> ' +
                    esc(t('files.provider_enabled', 'Enabled')) + '</label>' +
            '</div>' +
            '<div class="files-edit-field files-edit-field--check">' +
                '<label class="files-edit-check"><input type="checkbox" id="filesEditUpload-' + esc(id) + '"' + (p.upload_allowed ? ' checked' : '') + ' /> ' +
                    esc(t('files.provider_upload_allowed', 'Upload allowed')) + '</label>' +
            '</div>' +
            '<div class="files-edit-field files-edit-field--check">' +
                '<label class="files-edit-check"><input type="checkbox" id="filesEditDownload-' + esc(id) + '"' + (p.download_allowed ? ' checked' : '') + ' /> ' +
                    esc(t('files.provider_download_allowed', 'Download allowed')) + '</label>' +
            '</div>' +
            '<div class="files-edit-field">' +
                '<label for="filesEditMinTtl-' + esc(id) + '">' + esc(t('files.provider_ttl_min', 'Min TTL (seconds)')) + '</label>' +
                '<input type="number" id="filesEditMinTtl-' + esc(id) + '" min="1" step="1" value="' + esc(minTtl) + '" placeholder="' + esc(ttlPlaceholder) + '" autocomplete="off" />' +
            '</div>' +
            '<div class="files-edit-field">' +
                '<label for="filesEditMaxTtl-' + esc(id) + '">' + esc(t('files.provider_ttl_max', 'Max TTL (seconds)')) + '</label>' +
                '<input type="number" id="filesEditMaxTtl-' + esc(id) + '" min="1" step="1" value="' + esc(maxTtl) + '" placeholder="' + esc(ttlPlaceholder) + '" autocomplete="off" />' +
            '</div>'
        );
    }

    function openProviderEdit(id) {
        var p = providerById(id);
        if (!p) return;
        closeModal();
        var dialogId = 'files-provider-edit';
        var html =
            '<div class="files-modal-backdrop" data-files-action="modal-backdrop-close" role="presentation">' +
                '<div class="files-modal" role="dialog" aria-modal="true" aria-labelledby="' + dialogId + '-title">' +
                    '<div class="files-modal-header">' +
                        '<h3 class="files-modal-title" id="' + dialogId + '-title">✏️ ' + esc(t('files.edit_provider', 'Edit provider')) + '</h3>' +
                        '<button type="button" class="files-modal-close" data-files-action="modal-close" aria-label="' + esc(t('files.dialog_close', 'Close dialog')) + '">×</button>' +
                    '</div>' +
                    '<div class="files-modal-body">' +
                        '<div class="files-provider-edit-head">' + esc(p.origin || '') + ' <code>' + esc(groupFp(p.service_key_fingerprint)) + '</code></div>' +
                        providerEditFields(id) +
                        '<div id="filesProviderEditError" class="files-provider-edit-error" role="alert"></div>' +
                    '</div>' +
                    '<div class="files-modal-footer">' +
                        '<button type="button" class="files-modal-cancel" data-files-action="modal-cancel">' + esc(t('common.cancel', 'Cancel')) + '</button>' +
                        '<button type="button" class="files-action-btn" data-files-action="provider-save">' + esc(t('common.save', 'Save')) + '</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
        var el = openModalHtml(html, function () {});
        el.dataset.providerId = id;
    }

    function providerSave() {
        var el = state.dialog;
        var id = el && el.dataset ? el.dataset.providerId : null;
        var p = providerById(id);
        if (!p) return;

        var nameEl = getEl('filesEditName-' + id);
        var enabledEl = getEl('filesEditEnabled-' + id);
        var uploadEl = getEl('filesEditUpload-' + id);
        var downloadEl = getEl('filesEditDownload-' + id);
        var minEl = getEl('filesEditMinTtl-' + id);
        var maxEl = getEl('filesEditMaxTtl-' + id);
        var errEl = getEl('filesProviderEditError');

        function err(msg) { if (errEl) errEl.textContent = msg; }

        var displayName = nameEl ? nameEl.value.trim() : '';
        if (!displayName) {
            err(t('files.err_provider_display_name', 'Display name must not be empty.'));
            return;
        }

        var minRaw = minEl ? minEl.value.trim() : '';
        var maxRaw = maxEl ? maxEl.value.trim() : '';
        var minPresent = minRaw !== '';
        var maxPresent = maxRaw !== '';
        var minVal = null;
        var maxVal = null;

        if (minPresent) {
            minVal = parseInt(minRaw, 10);
            if (!Number.isFinite(minVal) || String(minVal) !== minRaw || minVal <= 0) {
                err(t('files.err_provider_ttl', 'TTL must be a positive whole number of seconds.'));
                return;
            }
        }
        if (maxPresent) {
            maxVal = parseInt(maxRaw, 10);
            if (!Number.isFinite(maxVal) || String(maxVal) !== maxRaw || maxVal <= 0) {
                err(t('files.err_provider_ttl', 'TTL must be a positive whole number of seconds.'));
                return;
            }
        }
        if (minPresent && maxPresent && minVal > maxVal) {
            err(t('files.err_provider_ttl_min_max', 'Minimum TTL must not exceed maximum TTL.'));
            return;
        }

        // Build ONE PATCH body with only the fields that actually changed; a
        // cleared TTL bound is sent as JSON null so the server clears it.
        var body = {};
        if (displayName !== (p.display_name || '')) body.display_name = displayName;
        if (enabledEl && !!enabledEl.checked !== !!p.enabled) body.enabled = !!enabledEl.checked;
        if (uploadEl && !!uploadEl.checked !== !!p.upload_allowed) body.upload_allowed = !!uploadEl.checked;
        if (downloadEl && !!downloadEl.checked !== !!p.download_allowed) body.download_allowed = !!downloadEl.checked;

        var oldMin = (p.min_ttl_seconds === null || p.min_ttl_seconds === undefined) ? null : p.min_ttl_seconds;
        var oldMax = (p.max_ttl_seconds === null || p.max_ttl_seconds === undefined) ? null : p.max_ttl_seconds;
        if (minPresent) {
            if (minVal !== oldMin) body.min_ttl_seconds = minVal;
        } else if (oldMin !== null) {
            body.min_ttl_seconds = null; // cleared
        }
        if (maxPresent) {
            if (maxVal !== oldMax) body.max_ttl_seconds = maxVal;
        } else if (oldMax !== null) {
            body.max_ttl_seconds = null; // cleared
        }

        if (Object.keys(body).length === 0) {
            // Nothing changed — no pointless PATCH; just dismiss.
            err('');
            closeModal(false);
            return;
        }

        // Poll the PATCH to terminal; on success the command tracker refreshes
        // the authoritative provider projection before reporting success.
        providerCommand('/api/mca/providers/' + encodeURIComponent(id), 'PATCH', body, {
            resourceKey: 'provider:' + id,
            queued: t('files.provider_updating', 'Updating provider…'),
            success: t('files.provider_updated', 'Provider updated'),
        });
        closeModal(false);
    }

    function renderProviderSettings() {
        var list = getEl('filesProvidersList');
        if (!list) return;
        if (!state.providers.length) {
            list.innerHTML = '<div class="files-empty">' + esc(t('files.no_providers', 'No Relay providers configured.')) + '</div>';
            return;
        }
        list.innerHTML = state.providers.map(function (p) {
            var busy = state.busy['provider:' + p.provider_id];
            var stateLabel = filesRelayStateLabel(p.state);
            var kind = p.kind === 'third_party' ? t('files.provider_kind_third_party', 'Third-party') : t('files.provider_kind_own', 'Own');
            var enabled = busy ? ' disabled' : '';
            return (
                '<div class="files-provider-item">' +
                    '<div class="files-provider-head">' +
                        '<span class="files-provider-name">' + esc(p.display_name || p.origin || p.provider_id) +
                            (p.is_default ? ' <span class="files-provider-default">★</span>' : '') + '</span>' +
                        '<span class="files-provider-state files-state-' + esc(p.state) + '">' + esc(stateLabel) + '</span>' +
                    '</div>' +
                    '<div class="files-provider-meta">' + esc(p.origin || '') + ' · ' + esc(kind) + '</div>' +
                    '<div class="files-provider-fp"><code>' + esc(groupFp(p.service_key_fingerprint)) + '</code></div>' +
                    '<div class="files-provider-fields">' +
                        providerField('files.provider_tls', 'TLS required', p.tls_required ? '✓' : '—') +
                        providerField('files.provider_upload_allowed', 'Upload allowed', p.upload_allowed ? '✓' : '—') +
                        providerField('files.provider_download_allowed', 'Download allowed', p.download_allowed ? '✓' : '—') +
                        providerField('files.provider_ttl_min', 'Min TTL', p.min_ttl_seconds ? fmtGrace(p.min_ttl_seconds) : '—') +
                        providerField('files.provider_ttl_max', 'Max TTL', p.max_ttl_seconds ? fmtGrace(p.max_ttl_seconds) : '—') +
                        providerField('files.provider_max_bytes', 'Max ciphertext bytes', fmtBytes(p.max_ciphertext_bytes)) +
                        providerField('files.provider_protocol', 'Protocol version', p.protocol_version || '—') +
                        providerField('files.provider_token', 'Token', p.upload_token_configured ? '✓' : '—') +
                    '</div>' +
                    '<div class="files-provider-actions">' +
                        '<button type="button" class="files-action-btn" data-files-action="provider-edit" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                            esc(t('files.edit', 'Edit')) + '</button>' +
                        (p.is_default ? '' :
                            '<button type="button" class="files-action-btn" data-files-action="provider-set-default" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                                esc(t('files.set_default', 'Set default')) + '</button>') +
                        '<button type="button" class="files-action-btn" data-files-action="provider-toggle" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                            esc(p.enabled ? t('files.disable', 'Disable') : t('files.enable', 'Enable')) + '</button>' +
                        '<button type="button" class="files-action-btn" data-files-action="provider-check" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                            esc(t('files.check', 'Check')) + '</button>' +
                        '<button type="button" class="files-action-btn is-danger" data-files-action="provider-remove" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                            esc(t('files.remove', 'Remove')) + '</button>' +
                    '</div>' +
                    '<div class="files-provider-token">' +
                        '<input type="password" id="filesToken-' + esc(p.provider_id) + '" autocomplete="off" placeholder="' +
                            esc(p.upload_token_configured ? t('files.token_set', 'Token set (enter to replace)') : t('files.token_placeholder', 'Upload token')) + '" />' +
                        '<button type="button" class="files-action-btn" data-files-action="provider-save-token" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                            esc(t('files.save_token', 'Save token')) + '</button>' +
                        (p.upload_token_configured ?
                            '<button type="button" class="files-action-btn" data-files-action="provider-clear-token" data-provider="' + esc(p.provider_id) + '"' + enabled + '>' +
                                esc(t('files.clear_token', 'Clear')) + '</button>' : '') +
                    '</div>' +
                '</div>'
            );
        }).join('');
    }

    function providerField(labelKey, fallback, value) {
        return '<div class="files-provider-field"><span class="files-provider-field-label">' +
            esc(t(labelKey, fallback)) + '</span><span class="files-provider-field-value">' + esc(value) + '</span></div>';
    }

    function providerProbe() {
        var origin = getEl('filesProviderOrigin');
        var result = getEl('filesProviderProbeResult');
        if (!origin || !origin.value.trim()) {
            toast(t('files.err_no_origin', 'Enter a provider URL'), 'error');
            return;
        }
        api('/api/mca/providers/probe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_url: origin.value.trim() }),
        }).then(function (r) {
            if (r.status !== 202 || !r.data || !r.data.command_id) {
                toast(filesErrorCode(r.data), 'error');
                return;
            }
            trackCommand(r.data.command_id, {
                resourceKey: 'provider-probe',
                queued: t('files.probing', 'Probing provider…'),
                success: t('files.probe_ok', 'Provider reached'),
                onSuccess: function (cmd) {
                    state.providerProbe = (cmd && cmd.result) || {};
                    if (result) renderProbeConfirm(state.providerProbe, result);
                },
                onUnknown: function () { /* probe lost across restart */ },
            });
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    function renderProbeConfirm(probe, resultEl) {
        if (!resultEl) return;
        var fp = probe.service_key_fingerprint || '';
        resultEl.innerHTML =
            '<div class="files-provider-probe">' +
                '<div class="files-provider-probe-line">' + esc(t('files.probe_origin', 'Origin')) + ': ' + esc(probe.origin || '') + '</div>' +
                '<div class="files-provider-probe-line">' + esc(t('files.probe_fingerprint', 'Service key fingerprint')) + ': <code>' + esc(groupFp(fp)) + '</code></div>' +
                '<div class="files-provider-probe-line">' + esc(t('files.probe_confirm_hint', 'Verify the fingerprint against your provider, then name and register it.')) + '</div>' +
                '<input type="text" id="filesProviderName" placeholder="' + esc(t('files.provider_name', 'Provider name')) + '" autocomplete="off" />' +
                '<label class="files-field files-field-inline"><select id="filesProviderKind">' +
                    '<option value="own">' + esc(t('files.provider_kind_own', 'Own')) + '</option>' +
                    '<option value="third_party">' + esc(t('files.provider_kind_third_party', 'Third-party')) + '</option>' +
                '</select></label>' +
                '<button type="button" class="files-action-btn" data-files-action="provider-register">' +
                    esc(t('files.register', 'Confirm & register')) + '</button>' +
            '</div>';
    }

    function providerRegister() {
        var probe = state.providerProbe;
        if (!probe || !probe.probe_id) return;
        var nameInput = getEl('filesProviderName');
        var kindInput = getEl('filesProviderKind');
        var displayName = (nameInput && nameInput.value.trim()) ? nameInput.value.trim() : (probe.origin || '');
        var kind = kindInput && kindInput.value ? kindInput.value : 'own';
        var fingerprint = probe.service_key_fingerprint || '';

        confirmDialog({
            title: t('files.confirm_register_title', 'Register provider?'),
            bodyHtml: '<div class="files-provider-probe-line">' + esc(t('files.probe_fingerprint', 'Service key fingerprint')) +
                ': <code>' + esc(groupFp(fingerprint)) + '</code></div>' +
                esc(t('files.confirm_register_body', 'Verify the fingerprint, then register this provider.')),
            confirmLabel: t('files.register', 'Confirm & register'),
            danger: false,
        }).then(function (yes) {
            if (!yes) return;
            api('/api/mca/providers', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    probe_id: probe.probe_id,
                    fingerprint_confirmation: fingerprint,
                    display_name: displayName,
                    policy: { kind: kind, upload_allowed: true, download_allowed: true },
                }),
            }).then(function (r) {
                if (r.status !== 202 || !r.data || !r.data.command_id) {
                    toast(filesErrorCode(r.data), 'error');
                    return;
                }
                trackCommand(r.data.command_id, {
                    resourceKey: 'provider-register',
                    queued: t('files.registering', 'Registering provider…'),
                    success: t('files.provider_registered', 'Provider registered'),
                    onSuccess: function () {
                        state.providerProbe = null;
                        var resultEl = getEl('filesProviderProbeResult');
                        if (resultEl) resultEl.innerHTML = '';
                        var originEl = getEl('filesProviderOrigin');
                        if (originEl) originEl.value = '';
                        loadProviders();
                    },
                    onUnknown: function () { loadProviders(); },
                });
            }).catch(function () {
                toast(t('files.error.network_error', 'Network error'), 'error');
            });
        });
    }

    // ---- bootstrap: register delegated listeners + module surface ----------

    function bindListeners() {
        if (typeof document === 'undefined') return;
        if (document.addEventListener) {
            document.addEventListener('click', onDocumentClick);
            document.addEventListener('keydown', onDocumentKeydown);
            document.addEventListener('input', onDocumentInput);
            document.addEventListener('change', onDocumentChange);
            document.addEventListener('visibilitychange', onVisibilityChange);
        }
        // Direct input listener for the search box (delegation covers dynamic content,
        // but the search box is static markup so bind it directly too for clarity).
        var search = getEl('filesSearch');
        if (search && search.addEventListener && !search._filesBound) {
            search._filesBound = true;
            search.addEventListener('input', onDocumentInput);
        }
    }

    bindListeners();

    window.MeshCenterFiles = {
        activate: activate,
        deactivate: deactivate,
        refresh: refresh,
    };

    // Static compatibility entry points referenced by the existing template
    // header buttons / filter markup (literal values only, no dynamic ids).
    window.openFilesWorkspace = activate;
    window.closeFilesWorkspace = deactivate;
    window.loadFilesWorkspace = refresh;
    window.setFilesFilter = setFilter;
    window.openFilesSendDialog = openSendDialog;
    window.closeFilesSendDialog = function () { closeModal(false); };
    window.openFilesProviderSettings = openProviderSettings;
    window.closeFilesProviderSettings = function () { closeModal(false); };
    // Entry point used by the Settings workspace "Relay providers" control: it
    // navigates to the Files workspace (which activates the module) and reuses
    // the SAME provider component — no second implementation or cache.
    window.openFilesRelayProviders = function () {
        if (typeof window.switchMainTab === 'function') window.switchMainTab('files');
        openProviderSettings();
    };
})();
