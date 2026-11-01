/* static/files.js
 *
 * The Files workspace - MeshCenter's end-to-end encrypted file-transfer UI
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
 * Every mutating request is a plain `fetch` - static/csrf.js monkey-patches
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
    var MAX_SEND_PICTURES = 10;           // gallery G2: pictures per send (one transfer each)
    var CONTACT_ID_RE = /^![0-9a-f]{8}$/;  // mirrors _CONTACT_ID_RE server-side

    // ---- backend state machines (mirrors meshsrv/attachments/{sender,receiver}.py) ----

    var SENDER_AUTOMATIC = ['DRAFT', 'VALIDATING', 'ENCRYPTING', 'QUEUED_UPLOAD',
        'UPLOADING', 'READY_TO_SEND'];
    var SENDER_REVOKABLE = ['SENT', 'RECEIVED', 'DOWNLOADED'];
    var RECEIVER_AUTOMATIC = ['WAITING_KEY', 'WAITING_PROVIDER', 'WAITING_NETWORK', 'DOWNLOADING'];

    var SENDER_TERMINAL = { DOWNLOADED: 1, EXPIRED: 1, REVOKED: 1, CANCELLED: 1,
        FAILED_VALIDATION: 1, FAILED_UPLOAD: 1, FAILED_RADIO: 1 };
    var RECEIVER_TERMINAL = { AVAILABLE: 1, EXPIRED: 1, REJECTED: 1, FAILED: 1 };

    // Attention states for the archive summary (C5 §10.3) - a transfer the
    // user is expected to act on. WAITING_NETWORK and success states are
    // deliberately excluded.
    var ATTENTION_STATES = { WAITING_CONSENT: 1, WAITING_KEY: 1, WAITING_PROVIDER: 1,
        FAILED_VALIDATION: 1, FAILED_UPLOAD: 1, FAILED_RADIO: 1, FAILED: 1 };

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

    // Step 5 ("Normal" detail level): the plan's 5-bucket plain-language
    // status, collapsing the raw backend state machine for a normal user.
    // Terminal/error states are deliberately NOT bucketed here - the plan
    // wants those "as-is, separate clear statuses" (they're already
    // plain-language via FILES_STATE_LABELS: "Rejected", "Expired",
    // "Cancelled", "Validation failed", etc.) rather than merged into one
    // generic "Failed"/"Done" bucket that would lose the distinction.
    var FILES_CONTACT_STATUS_LABELS = {
        trusted: 'Trusted',
        confirmation_required: 'Confirmation required',
        key_changed: 'Key changed',
        key_unknown: 'Key unknown',
    };

    // PR 4: key-request capability states (worker-published), and the
    // file-unavailability reasons from the centralized capability matrix.
    var FILES_KEY_REQUEST_STATE_LABELS = {
        idle: 'Key unknown',
        queued: 'Key request sent',
        waiting_response: 'Waiting for response',
        retry_available: 'Retry available',
        no_answer: 'No answer',
    };

    var FILES_UNAVAILABLE_REASON_LABELS = {
        local_node: 'This is your own node',
        channel_not_supported: 'Channels do not support file transfer',
        key_unknown: 'Key unknown',
        key_unverified: 'Key not yet verified',
        key_changed: 'Key changed',
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

    // UploadReadiness (config-only 3-value enum) - distinct from the richer
    // UploadRejectionReason map above, which shares `upload_token_missing`.
    var FILES_UPLOAD_READINESS_LABELS = {
        ready: 'Ready',
        upload_token_missing: 'No upload token configured',
        upload_disabled: 'Uploads disabled',
    };

    // G3: allowlist mapping from the backend's raw provider `last_error_code`
    // (see meshsrv/connectivity_monitor.py) to a localized label. Identity
    // findings and requests.RequestException subclass names are the only
    // classes the monitor persists; anything else falls back to a generic
    // "check failed" label so raw exception text is never rendered verbatim.
    var PROVIDER_ERROR_CODE_LABELS = {
        info_malformed: { key: 'files.provider_error.info_malformed', fallback: 'Provider info malformed' },
        provider_id_mismatch: { key: 'files.provider_error.provider_id_mismatch', fallback: 'Provider identity mismatch' },
        service_public_key_mismatch: { key: 'files.provider_error.service_public_key_mismatch', fallback: 'Provider key mismatch' },
        ConnectionError: { key: 'files.provider_error.connection_error', fallback: 'Connection failed' },
        ConnectTimeout: { key: 'files.provider_error.connect_timeout', fallback: 'Connection timed out' },
        ReadTimeout: { key: 'files.provider_error.read_timeout', fallback: 'Response timed out' },
        Timeout: { key: 'files.provider_error.timeout', fallback: 'Request timed out' },
        SSLError: { key: 'files.provider_error.ssl_error', fallback: 'TLS error' },
        TooManyRedirects: { key: 'files.provider_error.too_many_redirects', fallback: 'Too many redirects' },
        RequestException: { key: 'files.provider_error.request_error', fallback: 'Request failed' },
    };

    // ---- single internal state object --------------------------------------

    var state = {
        active: false,
        visible: true,
        epoch: 0,                 // bumped on activate()/deactivate() - stale reads are dropped
        refreshTimer: null,
        loading: {},              // per-resource in-flight guard (key -> Promise; joinable, G1)
        followUp: {},             // per-resource coalesced post-command authoritative read (G1)
        busy: {},                 // resourceKey -> commandId currently tracked
        contacts: [],             // node targets projected from the shared store (PR 4)
        providers: [],
        connectivity: { internet: 'unknown', relays: {} },
        settings: null,
        localNodeId: '',
        storeSubscribed: false,    // PR 4 Finding 1: one subscription to the shared store

        attachments: [],
        total: 0,
        truncated: false,
        counterparty: '',         // active counterparty filter (canonical !hex contact id); '' = none (P3)
        lastTransfersQueryKey: '', // last query applied by loadTransfers
        search: '',
        segment: 'all',           // FX1: incoming / sent / all, remembered per device
        pendingSelectId: null,    // a transfer to bring into view as soon as the list has it (MCA-UX1)
        focusId: '',              // the card to scroll to and flash after the next render
        selectMode: false,        // FX1: multi-select (long press on touch, the Select button elsewhere)
        checked: {},              // id -> true while selecting
        openMore: {},             // id -> true: the "More details" of this card is open (survives a poll re-render)
        openComment: {},          // id -> true: the full comment is shown
        openCard: {},             // id -> true: the phone card is expanded (actions and details shown)
        searchOpen: false,        // phone: the search field replaces the toolbar
        moreOpen: false,          // phone: the more menu is open
        moreExtra: {},            // id -> timeline / deliveries html fetched when "More details" was first opened
        chatScrolledFor: '',      // the contact the messenger layout was last scrolled to the bottom for
        storage: null,            // {count, bytes}: the received copies in cache/incoming (MCA-UX1)

        sendSignature: null,      // semantic send-form signature (C7 idempotency)
        sendClientRequestId: null,
        sendInFlight: false,
        sendPictures: null,     // gallery G2: {items, total, resize, onDone} while the Send dialog sends pictures
        sendGeneration: 0,        // bumped per send-dialog open/close - isolates generations (G2)
        sendReadinessToken: 0,    // monotonic token - stale readiness responses dropped (F2)
        sendReadiness: null,      // authoritative readiness result for the open send dialog
        sendTtlDebounce: null,    // debounce timer for rapid Custom-TTL typing (F2)

        dialog: null,             // currently-open dialog element
        dialogReturnFocus: null,  // element to restore focus to on close (C11)
        modalSeq: 0,              // id suffix for auto-wired aria-describedby (R6)
        providerProbe: null,      // last successful probe result (pending registration)
        relayWizard: null,        // active step-by-step Relay setup wizard, or null when closed
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
    function filesKeyRequestStateLabel(s) { return t('files.key_request_state.' + s, FILES_KEY_REQUEST_STATE_LABELS[s] || s); }
    function fileUnavailableReasonLabel(s) { return t('files.unavailable.' + s, FILES_UNAVAILABLE_REASON_LABELS[s] || s || 'Unknown'); }
    function filesRelayStateLabel(s) { return t('files.relay_state.' + s, FILES_RELAY_STATE_LABELS[s] || s); }
    function filesReadinessLabel(s) { return t('files.ready_reason.' + s, FILES_READINESS_LABELS[s] || s || 'Unknown'); }
    function filesUploadReadinessLabel(s) { return t('files.upload_readiness.' + s, FILES_UPLOAD_READINESS_LABELS[s] || s || 'Unknown'); }

    function filesErrorCode(data) {
        var code = (data && data.error_code) ? data.error_code : 'internal_error';
        var fallback = (data && data.error) || code;
        var known = t('files.error.' + code, '');
        if (known) return known;
        // a request the page itself should never send badly (invalid_id, invalid_filter ...): one translated line
        if (code.indexOf('invalid_') === 0) return t('files.error.invalid_request', 'The request was not valid');
        return fallback;
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
            // the vault locked while the page was open (or the page is older than the lock): let the chip and banners catch up
            if (resp && resp.status === 423 && data && data.error_code === 'vault_locked' && typeof window !== 'undefined' && window.MCVault) window.MCVault.noteLocked();
            return { status: resp ? resp.status : 0, data: data || { ok: false, error: 'HTTP error', error_code: 'http_error' } };
        });
    }

    // the shared formatter (static/format-size.js): localized units, the same everywhere in the UI
    function fmtBytes(n) {
        if (typeof window !== 'undefined' && window.MCSize) return window.MCSize.format(n);
        if (n === null || n === undefined) return '-';
        if (n < 1024) return n + ' B';
        if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
        return (n / (1024 * 1024)).toFixed(1) + ' MB';
    }

    function fmtDate(epochSeconds) {
        if (!epochSeconds) return '-';
        var d = new Date(epochSeconds * 1000);
        return d.toLocaleString ? d.toLocaleString() : String(d);
    }

    function fmtRel(epochSeconds) {
        if (!epochSeconds) return '-';
        var now = Math.floor(Date.now() / 1000);
        var s = epochSeconds - now;
        if (s < 0) return t('files.expired', 'Expired');
        if (s < 3600) return Math.max(1, Math.round(s / 60)) + 'm';
        if (s < 86400) return Math.round(s / 3600) + 'h';
        return Math.round(s / 86400) + 'd';
    }

    function fmtGrace(seconds) {
        if (!seconds) return '-';
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
        // Step 6 regression finding: a genuine send-side failure
        // (radio_send_failed, relay_unavailable, ...) bounces the attachment
        // back into an ordinary in-progress state (READY_TO_SEND,
        // QUEUED_UPLOAD) rather than a terminal FAILED_* one, and gets
        // retried indefinitely - none of those in-progress states are in
        // ATTENTION_STATES, so without this the archive's "N need attention"
        // count would never reflect it. Any transfer carrying a genuine
        // error_code counts, regardless of which state it's currently in.
        return Boolean(a.error_code);
    }

    function hasActiveCommand() {
        return Object.keys(state.busy).length > 0;
    }

    function canonicalNodeId(id) {
        return String(id || '').toLowerCase();
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

    // ---- command tracker (C2) - one generic path for every 202 {command_id} ----

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
                        // still pending - keep polling, no notification churn
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
        // G1: the guard is a JOINABLE promise, not a boolean - a caller that
        // fires while the same resource is already loading gets the in-flight
        // promise back (and waits on it) instead of an instantly-resolved one,
        // so a terminal callback can never announce completion ahead of the
        // authoritative read it is waiting on. The key is released only after
        // the promise fully settles, so an awaiter observes the guard as free.
        if (state.loading[key]) return state.loading[key];
        var epoch = state.epoch;
        var promise = fetcher().then(function (result) {
            // The Send dialog can be opened from the Media gallery while the Files workspace was never activated
            if (epoch === state.epoch && (state.active || isSendDialogOpen())) onDone(result);
        }).catch(function () {
            // a failed read leaves the projection unchanged; the guard is still
            // released below so the next read is not permanently blocked
        }).then(function () {
            if (state.loading[key] === promise) delete state.loading[key];
        });
        state.loading[key] = promise;
        return promise;
    }

    // G1: a command's terminal projection refresh. Join any read already in
    // flight for `key` (so a command that settles mid-refresh cannot announce
    // success/unknown before that authoritative read lands), then issue exactly
    // one fresh read so the projection reflects the just-committed command.
    // Coalesces concurrent terminal callbacks for the same resource onto a
    // single follow-up read.
    function refreshAfterCommand(key, loader) {
        var prior = state.loading[key] || state.followUp[key];
        var joined = prior ? prior.then(function () {}, function () {}) : Promise.resolve();
        var read = joined.then(loader);
        state.followUp[key] = read;
        read.then(function () {
            if (state.followUp[key] === read) delete state.followUp[key];
        }, function () {
            if (state.followUp[key] === read) delete state.followUp[key];
        });
        return read;
    }

    function refreshContacts() {
        // PR 4: the contact list is now a projection of the shared target
        // store (static/targets.js), which merges nodes + MCA bindings +
        // key-request capability + the cached channel projection + local node
        // identity into one model. files.js no longer re-merges the raw
        // endpoints itself.
        var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
        if (!store) return Promise.resolve();
        return store.refresh().then(function () {
            syncContactsFromStore();
            refreshSendRecipientsIfOpen();
        });
    }

    function refreshContactsAfterCommand() {
        // PR 5 final correction (Finding 2): a contact command's terminal
        // callback must NOT run through files.js's own refreshAfterCommand key
        // (that guard is files-local and never tracks the store's in-flight
        // sources, so `store.refresh()` would JOIN a pre-command read and
        // announce success on a stale projection). The store owns the joinable
        // post-command API: join its in-flight sources, then force one fresh
        // read, then re-sync files.js's local projection.
        var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
        if (!store || typeof store.refreshAfterCommand !== 'function') {
            return refreshContacts();
        }
        return store.refreshAfterCommand().then(function () {
            syncContactsFromStore();
            refreshSendRecipientsIfOpen();
        });
    }

    // Map the shared store's node targets onto the internal `state.contacts`
    // projection (PR 5: no longer rendered as a list - it backs the Send-dialog
    // recipients and contactForAttachment() lookups only). The local node is
    // excluded and the centralized capability fields (can_send_file /
    // can_request_key / key_request_state / file_unavailable_reason) are carried
    // through rather than re-derived per component.
    function syncContactsFromStore() {
        var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
        if (!store) return;
        var localId = canonicalNodeId(store.localNodeId());
        if (localId) state.localNodeId = localId;
        var targets = store.nodeTargets();
        state.contacts = targets.filter(function (t) {
            return !t.is_local;
        }).map(function (t) {
            return {
                contact_id: t.id,
                status: t.contact_status || 'key_unknown',
                trust_state: t.trust_state,
                key_request_state: t.key_request_state,
                can_send_file: t.can_send_file,
                can_request_key: t.can_request_key,
                file_unavailable_reason: t.file_unavailable_reason,
                fingerprint: t.fingerprint || '',
                pending_fingerprint: t.pending_fingerprint || '',
                key_epoch: t.key_epoch,
                pending_key_epoch: t.pending_key_epoch,
                name: t.display_name || '',
                short_name: t.short_name || '',
                favorite: t.favorite === true,
                hasBinding: t.hasBinding,
            };
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
        return guardedLoad('connectivity', function () {
            return api('/api/mca/connectivity');
        }, function (r) {
            if (r.status === 200 && r.data && r.data.ok) {
                state.connectivity = {
                    internet: r.data.internet || 'unknown',
                    relays: r.data.relays || {},
                };
            }
            // G2: workspace-level loads must never drive a Send dialog's status
            // or readiness - only the send dialog's own generation-scoped loads
            // (loadConnectivityForSend) update an open Send dialog.
        });
    }

    function loadSettings() {
        return guardedLoad('settings', function () {
            return api('/api/settings');
        }, function (r) {
            if (r.status === 200 && r.data && r.data.ok && r.data.settings) {
                state.settings = r.data.settings;
            }
            // G2: see loadConnectivity - no Send-dialog side effects here.
        });
    }

    // ---- the list: always the whole history of the selected contact (or of everybody); the segment narrows it in the page --
    function transfersQueryKey() {
        return 'all|all|' + (state.counterparty || '');
    }

    function transfersUrl() {
        var url = '/api/attachments?direction=all&filter=all&limit=' + LIST_LIMIT;
        if (state.counterparty) url += '&counterparty=' + encodeURIComponent(state.counterparty);
        return url;
    }

    function loadTransfers() {
        var url = transfersUrl();
        var queryKey = transfersQueryKey();
        // P3: the guard key carries the epoch so a reactivated tab never joins a request left in flight by a previous activation
        var key = 'transfers:' + state.epoch + ':' + queryKey;
        state.lastTransfersQueryKey = queryKey;
        return guardedLoad(key, function () {
            return api(url);
        }, function (r) {
            if (transfersQueryKey() !== queryKey) return; // superseded by another contact: drop, including a stale error
            if (r.status !== 200 || !r.data || r.data.ok !== true) {
                if (r.status === 503) renderTransfersError(t('files.not_ready', 'MCAttach service is not ready'));
                else renderTransfersError(filesErrorCode(r.data));
                return;
            }
            state.attachments = Array.isArray(r.data.attachments) ? r.data.attachments : [];
            state.total = r.data.total || 0;
            state.truncated = state.total > LIST_LIMIT;
            if (state.pendingSelectId && state.attachments.some(function (a) { return a.id === state.pendingSelectId; })) {
                var pending = state.attachments.filter(function (a) { return a.id === state.pendingSelectId; })[0];
                if (state.segment !== 'all' && segmentOf(pending) !== state.segment) { state.segment = 'all'; writeSegment('all'); }
                state.focusId = state.pendingSelectId;   // MCA-UX1: opened from a chat bubble
                state.pendingSelectId = null;
            }
            renderTransfers();
            renderFooter();
            noteFilesSeen();
        });
    }

    // P3: the post-command authoritative refresh keys off the current epoch + query
    function refreshTransfers() {
        return refreshAfterCommand('transfers:' + state.epoch + ':' + transfersQueryKey(), loadTransfers);
    }

    // ---- contact lookup (projection of the shared target store, PR 4) ------

    function contactByNameOrId(c, contactId) {
        return canonicalNodeId(c.contact_id) === canonicalNodeId(contactId);
    }

    function findContact(contactId) {
        for (var i = 0; i < state.contacts.length; i++) {
            if (contactByNameOrId(state.contacts[i], contactId)) return state.contacts[i];
        }
        // PR 5 fallback: the MCA key actions now live in the sidebar node card
        // (rendered by chat.js from the shared store), not in a Files list. If
        // the contact isn't in `state.contacts` yet (Files hasn't refreshed),
        // resolve name/fingerprint straight from the shared store's node target
        // so a key action fired from the sidebar still gets the full detail.
        var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
        if (!store || typeof store.getNode !== 'function') return null;
        var t = store.getNode(contactId);
        if (!t) return null;
        return {
            contact_id: t.id,
            status: t.contact_status || 'key_unknown',
            trust_state: t.trust_state,
            key_request_state: t.key_request_state,
            can_send_file: t.can_send_file,
            can_request_key: t.can_request_key,
            file_unavailable_reason: t.file_unavailable_reason,
            fingerprint: t.fingerprint || '',
            pending_fingerprint: t.pending_fingerprint || '',
            key_epoch: t.key_epoch,
            pending_key_epoch: t.pending_key_epoch,
            name: t.display_name || '',
            hasBinding: t.hasBinding,
        };
    }

    // ---- rendering: archive (C5) -------------------------------------------

    // ---- rendering: detail + action matrix (C4/C6) -------------------------

    // ---- FX1 Phase A: plain statuses ---------------------------------------------------------------------------------
    // The ONE place that turns the internal state machine into what a person reads. Internal codes only appear under
    // "More details" in the card. tests/frontend/test_files_status.mjs pins this table.
    var PLAIN_STATUS = {
        sent: {
            DRAFT: 'sending', VALIDATING: 'sending', ENCRYPTING: 'sending', QUEUED_UPLOAD: 'sending', UPLOADING: 'sending',
            READY_TO_SEND: 'sending',
            SENT: 'waiting_recipient', RECEIVED: 'waiting_recipient',
            DOWNLOADED: 'delivered',
            EXPIRED: 'expired', REVOKED: 'revoked', CANCELLED: 'cancelled', REJECTED: 'declined',
            FAILED_VALIDATION: 'not_delivered', FAILED_UPLOAD: 'not_delivered', FAILED_RADIO: 'not_delivered',
        },
        received: {
            OFFER_RECEIVED: 'waiting_in', WAITING_KEY: 'waiting_key', WAITING_PROVIDER: 'waiting_relay',
            WAITING_NETWORK: 'waiting_internet', WAITING_CONSENT: 'waiting_ok',
            DOWNLOADING: 'downloading', VERIFYING: 'downloading',
            AVAILABLE: 'received',
            EXPIRED: 'expired_in', REJECTED: 'declined_in', CANCELLED: 'cancelled_in', FAILED: 'not_received',
        },
    };
    var PLAIN_TONE = {
        sending: 'progress', waiting_recipient: 'wait', delivered: 'ok', expired: 'bad', revoked: 'bad', cancelled: 'bad',
        declined: 'bad', not_delivered: 'bad',
        waiting_in: 'wait', waiting_key: 'wait', waiting_relay: 'wait', waiting_internet: 'wait', waiting_ok: 'wait',
        downloading: 'progress', received: 'ok', expired_in: 'bad', declined_in: 'bad', cancelled_in: 'bad', not_received: 'bad',
    };
    var PLAIN_LABELS = {
        sending: 'Sending', waiting_recipient: 'Waiting for recipient', delivered: 'Delivered', expired: 'Not delivered: the link expired',
        revoked: 'Cancelled by you', cancelled: 'Cancelled', declined: 'Not delivered: the recipient declined',
        not_delivered: 'Not delivered',
        waiting_in: 'Waiting', waiting_key: "Waiting for the sender's key", waiting_relay: 'Waiting for the relay',
        waiting_internet: 'Waiting for the internet', waiting_ok: 'Waiting for your OK',
        downloading: 'Downloading', received: 'Received', expired_in: "Not received: the sender's link expired",
        declined_in: 'You declined this file', cancelled_in: 'Cancelled by the sender', not_received: 'Not received',
    };
    // an error code that only means "trying again automatically": the status stays "Sending" with a muted note
    var RETRYING_CODES = { relay_unavailable: 1, radio_send_failed: 1, radio_send_not_confirmed: 1 };
    var REASON_BY_CODE = {
        recipient_not_trusted: 'key_not_trusted', no_recipients: 'no_recipient', source_file_missing: 'source_missing',
        disk_low_block: 'disk_low', recipient_provider_unknown: 'recipient_relay',
        mime_not_allowed: 'type_not_allowed', extension_not_allowed: 'type_not_allowed', type_mismatch: 'type_mismatch',
        pending_offer_signature_invalid: 'sender_unverified', provider_missing: 'no_relay',
        chunk_fetch_failed: 'download_failed',
        descriptor_signature_invalid: 'damaged', manifest_invalid: 'damaged', header_size_mismatch: 'damaged',
        chunk_digest_mismatch: 'damaged', chunk_aead_failed: 'damaged', plaintext_digest_mismatch: 'damaged',
    };
    var REASON_LABELS = {
        type_mismatch: 'the file is not the type it claims to be',
        key_not_trusted: "the contact's key is not trusted", no_recipient: 'there is no recipient', source_missing: 'the file is no longer on this node',
        disk_low: 'not enough free space on this node', recipient_relay: 'the recipient does not use this relay',
        type_not_allowed: 'this file type is not accepted', sender_unverified: 'the sender could not be verified',
        no_relay: 'no relay is set up here', download_failed: 'the download failed', damaged: 'the file was damaged or not authentic',
        relay_refused: 'the relay refused the file', unknown: 'something went wrong',
    };

    function reasonKeyFor(code) {
        if (!code) return '';
        if (REASON_BY_CODE[code]) return REASON_BY_CODE[code];
        if (/^(relay_http_|create_upload_http_|relay_)/.test(code)) return 'relay_refused';
        return 'unknown';
    }

    function reasonText(code) {
        var key = reasonKeyFor(code);
        return key ? t('files.reason.' + key, REASON_LABELS[key]) : '';
    }

    // {key, tone, text, retrying, busy}: key is the plain status, text what the chip says (with the reason for a failure)
    function plainStatus(a) {
        var side = a.direction === 'sent' ? 'sent' : 'received';
        var key = PLAIN_STATUS[side][a.state] || (side === 'sent' ? 'sending' : 'waiting_in');
        // a recipient that cannot use this relay will never get the file: a state of its own, not "waiting"
        if (side === 'sent' && a.error_code === 'recipient_provider_unknown' && !SENDER_TERMINAL[a.state]) key = 'not_delivered';
        var retrying = Boolean(key === 'sending' && a.error_code && RETRYING_CODES[a.error_code]);
        var text = t('files.status.' + key, PLAIN_LABELS[key]);
        if ((key === 'not_delivered' || key === 'not_received') && a.error_code) {
            var reason = reasonText(a.error_code);
            if (reason) {
                text = tparams('files.status.' + key + '_reason', { reason: reason }, PLAIN_LABELS[key] + ': ' + reason);
            }
        }
        var tone = PLAIN_TONE[key] || 'wait';
        // a short word for a narrow phone (a tooltip does not exist on touch); the full text is shown in the opened card
        return { key: key, tone: tone, text: text, short: t('files.status_short.' + key, text), retrying: retrying, busy: tone === 'progress' };
    }

    // "Link valid for 2 more days": only while the file still waits (nobody has it yet)
    function plural(key, count, fallbackOne, fallbackOther) {
        if (typeof window !== 'undefined' && window.I18N && typeof window.I18N.tOrFallback === 'function' &&
            window.I18N.tOrFallback(key, {}, null) !== null && typeof window.I18N.plural === 'function') {
            return window.I18N.plural(key, count, {});
        }
        return (count === 1 ? fallbackOne : fallbackOther).replace('{count}', String(count));
    }

    function linkValidText(a) {
        if (!a.hard_expires_at) return '';
        var key = plainStatus(a).key;
        if (key !== 'waiting_recipient' && key !== 'waiting_in' && key !== 'waiting_key' && key !== 'waiting_relay' &&
            key !== 'waiting_internet' && key !== 'waiting_ok') return '';
        var left = a.hard_expires_at - Math.floor(Date.now() / 1000);
        if (left <= 0) return '';
        if (left >= 2 * 86400) {
            var days = Math.round(left / 86400);
            return plural('files.link_valid_days', days, 'Link valid for {count} more day', 'Link valid for {count} more days');
        }
        if (left >= 2 * 3600) {
            var hours = Math.round(left / 3600);
            return plural('files.link_valid_hours', hours, 'Link valid for {count} more hour', 'Link valid for {count} more hours');
        }
        var minutes = Math.max(1, Math.round(left / 60));
        return plural('files.link_valid_minutes', minutes, 'Link valid for {count} more minute', 'Link valid for {count} more minutes');
    }

    // ---- executable files: a short warning before they leave the node --------------------------------------------------
    var EXECUTABLE_EXT = /\.(exe|msi|apk|sh|bat|cmd|ps1|jar|js|vbs|scr|com|app|dmg|pkg|deb|rpm|bin|run|py|pl|php|lnk|reg|hta|wsf|cpl|msc|gadget|appimage)$/i;
    function isExecutableName(name) { return Boolean(name && EXECUTABLE_EXT.test(String(name))); }

    // ---- segments, grouping, cards -----------------------------------------------------------------------------------
    var SEGMENT_KEY = 'mc.files.segment';
    function readSegment() {
        try {
            var v = window.localStorage.getItem(SEGMENT_KEY);
            return (v === 'incoming' || v === 'sent' || v === 'all') ? v : 'all';
        } catch (e) { return 'all'; }
    }
    function writeSegment(v) { try { window.localStorage.setItem(SEGMENT_KEY, v); } catch (e) { /* private window: not remembered */ } }

    function segmentOf(a) { return a.direction === 'sent' ? 'sent' : 'incoming'; }

    function segmentCounts(list) {
        var c = { incoming: 0, sent: 0, all: list.length };
        list.forEach(function (a) { c[segmentOf(a)]++; });
        return c;
    }

    function visibleAttachments() {
        var q = state.search.trim().toLowerCase();
        return state.attachments.filter(function (a) {
            if (state.segment !== 'all' && segmentOf(a) !== state.segment) return false;
            if (!q) return true;
            var contact = contactForAttachment(a);
            var hay = [a.file_name || '', (contact && contact.name) || '', (contact && contact.contact_id) || '',
                a.comment || '', plainStatus(a).text].join(' ').toLowerCase();
            return hay.indexOf(q) !== -1;
        });
    }

    function contactForAttachment(a) {
        // §7.5 counterparty_contact_id: a canonical transport address the worker derives from routing data, never from
        // recipient.principal_id (a different namespace). Null means there is no trustworthy mapping: never guess.
        if (!a.counterparty_contact_id) return null;
        return findContact(a.counterparty_contact_id);
    }

    function providerById(pid) {
        for (var i = 0; i < state.providers.length; i++) {
            if (state.providers[i].provider_id === pid) return state.providers[i];
        }
        return null;
    }

    function dayStart(ms) { var d = new Date(ms); d.setHours(0, 0, 0, 0); return d.getTime(); }

    function dayLabel(epochSeconds) {
        var ms = epochSeconds * 1000;
        var today = dayStart(Date.now());
        var that = dayStart(ms);
        var diff = Math.round((today - that) / 86400000);
        if (diff === 0) return t('files.day_today', 'Today');
        if (diff === 1) return t('files.day_yesterday', 'Yesterday');
        var locale = (typeof window !== 'undefined' && window.I18N && window.I18N.locale) || undefined;
        try {
            return new Date(ms).toLocaleDateString(locale, { weekday: 'long', day: 'numeric', month: 'long', year: diff > 300 ? 'numeric' : undefined });
        } catch (e) { return new Date(ms).toDateString(); }
    }

    function clock(epochSeconds) {
        var locale = (typeof window !== 'undefined' && window.I18N && window.I18N.locale) || undefined;
        try { return new Date(epochSeconds * 1000).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' }); } catch (e) { return ''; }
    }

    // newest first, or oldest first like a messenger when one contact is selected
    function groupByDay(list, ascending) {
        var sorted = list.slice().sort(function (a, b) { return ascending ? a.created_at - b.created_at : b.created_at - a.created_at; });
        var groups = [];
        sorted.forEach(function (a) {
            var key = dayStart((a.created_at || 0) * 1000);
            var last = groups[groups.length - 1];
            if (!last || last.key !== key) {
                last = { key: key, label: dayLabel(a.created_at || 0), items: [] };
                groups.push(last);
            }
            last.items.push(a);
        });
        return groups;
    }

    function thumbOk(a) {
        if (a.locked || !a.content_available || !IMAGE_MIME[a.mime_type]) return false;
        return !(a.direction === 'sent' && a.state === 'DOWNLOADED');       // after Delivered the card shows the type icon
    }

    function whoLine(a) {
        var contact = contactForAttachment(a);
        var name = contact ? (contact.name || contact.contact_id) : (a.counterparty_contact_id || '');
        if (!name) return a.direction === 'sent' ? t('files.sent', 'Sent') : t('files.received', 'Received');
        return a.direction === 'sent'
            ? tparams('files.card.sent_to', { name: name }, 'Sent to ' + name)
            : tparams('files.card.received_from', { name: name }, 'Received from ' + name);
    }

    function cardActionsHtml(a) {
        var out = [];
        var more = [];
        if (a.locked) {
            return { primary: '<button type="button" class="files-action-btn" data-vault-action="unlock">' + esc(t('vault.unlock', 'Unlock')) + '</button>', more: '' };
        }
        if (a.direction === 'sent') {
            if (SENDER_AUTOMATIC.indexOf(a.state) !== -1) {
                out.push(actionBtn(a.id, 'attach-retry', t('files.retry', 'Retry'), false));
                out.push(actionBtn(a.id, 'attach-cancel', t('files.cancel', 'Cancel'), true));
            } else if (SENDER_REVOKABLE.indexOf(a.state) !== -1) {
                more.push(actionBtn(a.id, 'attach-revoke', t('files.revoke', 'Revoke'), true));
            }
            if (a.content_available) {
                if (IMAGE_MIME[a.mime_type]) out.push(actionBtn(a.id, 'attach-open', t('files.open_preview', 'Open'), false));
                out.push(actionBtn(a.id, 'attach-download-device', t('files.download_to_device', 'Download to device'), false));
            }
        } else if (RECEIVER_AUTOMATIC.indexOf(a.state) !== -1) {
            out.push(actionBtn(a.id, 'attach-retry', t('files.retry', 'Retry'), false));
        } else if (a.state === 'WAITING_CONSENT') {
            out.push(actionBtn(a.id, 'attach-download', t('files.accept_download', 'Download'), false));
            out.push(actionBtn(a.id, 'attach-reject', t('files.decline', 'Decline'), true));
        } else if (a.state === 'AVAILABLE' && a.content_available) {
            if (IMAGE_MIME[a.mime_type]) out.push(actionBtn(a.id, 'attach-open', t('files.open_preview', 'Open'), false));
            out.push(actionBtn(a.id, 'attach-download-device', t('files.download_to_device', 'Download to device'), false));
            out.push(actionBtn(a.id, 'attach-delete-local', t('files.delete', 'Delete'), true));
        }
        return { primary: out.join(''), more: more.join('') };
    }

    function moreRowsHtml(a) {
        var provider = providerById(a.provider_id);
        var rows = [
            ['files.detail_file', 'File', a.locked ? '-' : (a.file_name || '-')],
            ['files.more_state', 'Internal state', filesStateLabel(a.state)],
            ['files.detail_created', 'Created', fmtDate(a.created_at)],
            ['files.detail_expires', 'Relay link expires', a.hard_expires_at ? fmtDate(a.hard_expires_at) + ' (' + fmtRel(a.hard_expires_at) + ')' : '-'],
            ['files.detail_provider', 'Relay used', provider ? (provider.display_name || provider.origin || a.provider_id) : (a.provider_id || '-')],
            ['files.tech_id', 'ID', a.id],
            ['files.detail_type', 'Type', a.mime_type || '-'],
            ['files.detail_route', 'Route', t('files.detail_direct', 'Direct')],
            ['files.detail_grace', 'Download grace', fmtGrace(a.download_grace_seconds)],
            ['files.tech_cipher_size', 'Ciphertext size', fmtBytes(a.cipher_size)],
            ['files.tech_provider_id', 'Provider ID', a.provider_id || '-'],
            ['files.tech_delivery_id', 'Primary delivery', a.primary_delivery_id || '-'],
        ];
        if (a.error_code) rows.push(['files.tech_error_code', 'Raw error code', a.error_code]);
        return detailRowsHtml(rows);
    }

    var CHEVRON = '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false"><path d="M6 9l6 6 6-6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

    // Phone: collapsed = three lines (who, name, size + time + status); a tap opens the rest (the extra block). Elsewhere the
    // extra block is always shown. One markup, the CSS decides.
    function cardHtml(a) {
        var status = plainStatus(a);
        var actions = cardActionsHtml(a);
        var incoming = a.direction !== 'sent';
        var name = a.locked ? '🔒 ' + t('vault.locked_name', 'Locked') : (a.file_name || t('files.encrypted_file', 'Encrypted file'));
        var thumb = thumbOk(a)
            ? '<img data-thumb loading="lazy" alt="" src="/api/attachments/' + esc(encodeURIComponent(a.id)) + '/thumbnail">'
            : mimeIcon(a.mime_type);
        var valid = linkValidText(a);
        var checked = state.checked[a.id] ? ' is-checked' : '';
        var open = state.openMore[a.id] ? ' open' : '';
        var expanded = state.openCard[a.id] ? ' is-open' : '';
        var comment = (!a.locked && a.comment)
            ? '<div class="files-card-comment' + (state.openComment[a.id] ? ' is-open' : '') + '" data-files-action="toggle-comment" data-attachment="' + esc(a.id) + '">' + esc(a.comment) + '</div>'
            : '';
        var busy = status.busy ? '<div class="files-progress" role="progressbar" aria-label="' + esc(status.text) + '"><span></span></div>' : '';
        var note = status.retrying ? '<span class="files-card-note">' + esc(t('files.note_retrying', 'trying again automatically')) + '</span>' : '';
        if (valid) note += '<span class="files-card-note">' + esc(valid) + '</span>';
        if (a.state === 'WAITING_CONSENT' && a.error_code === 'auto_accept_quota') {
            // not a failure: a trusted contact sent a lot today, so this one waits for a person
            note += '<span class="files-card-note">' + esc(t('files.note_auto_quota', 'Not downloaded automatically: this contact sent a lot today. Press Download to accept it.')) + '</span>';
        }
        if (a.state === 'WAITING_KEY') {
            note += '<span class="files-card-note">' + esc(t('files.note_confirm_key', "Confirm this contact's key in the node list to receive the file.")) + '</span>';
        }
        var noteBlock = (note || busy) ? '<div class="files-card-status">' + note + busy + '</div>' : '';
        var more = '<details class="files-card-more" data-more="' + esc(a.id) + '"' + open + '><summary>' + esc(t('files.more_details', 'More details')) + '</summary>' +
            '<div class="files-detail-table">' + moreRowsHtml(a) + '</div>' +
            (actions.more ? '<div class="files-card-actions">' + actions.more + '</div>' : '') +
            '<div class="files-more-extra" data-more-extra="' + esc(a.id) + '">' + (state.moreExtra[a.id] || '') + '</div></details>';
        return '<article class="files-card ' + (incoming ? 'is-incoming' : 'is-sent') + checked + expanded + '" id="filesCard-' + esc(a.id) + '" data-attachment="' + esc(a.id) + '" data-direction="' + (incoming ? 'incoming' : 'sent') + '">' +
            '<label class="files-check"><input type="checkbox" data-files-check="' + esc(a.id) + '"' + (state.checked[a.id] ? ' checked' : '') +
                ' aria-label="' + esc(t('files.select_one', 'Select')) + '"></label>' +
            '<div class="files-card-thumb">' + thumb + '</div>' +
            '<div class="files-card-body">' +
                '<div class="files-card-who">' + esc(whoLine(a)) + '</div>' +
                '<div class="files-card-name" title="' + esc(a.locked ? '' : (a.file_name || '')) + '">' + esc(name) + '</div>' +
                '<div class="files-card-line3"><span class="files-card-meta">' + esc(fmtBytes(a.plain_size)) + ' · ' + esc(clock(a.created_at || 0)) + '</span>' +
                    '<span class="files-chip is-' + esc(status.tone) + '" title="' + esc(status.text) + '"><span class="files-chip-full">' + esc(status.text) + '</span><span class="files-chip-short">' + esc(status.short) + '</span></span></div>' +
                '<div class="files-card-extra">' +
                    comment + noteBlock +
                    (actions.primary ? '<div class="files-card-actions">' + actions.primary + '</div>' : '') +
                    more +
                '</div>' +
            '</div>' +
            '<button type="button" class="files-card-chev" data-files-action="toggle-card" data-attachment="' + esc(a.id) + '" aria-expanded="' + (state.openCard[a.id] ? 'true' : 'false') +
                '" aria-label="' + esc(t('files.card_toggle', 'Show details')) + '">' + CHEVRON + '</button>' +
        '</article>';
    }

    function renderTransfersError(message) {
        var list = getEl('filesArchiveList');
        if (!list) return;
        list.innerHTML = '<div class="files-empty">' + esc(message) + '</div>';
    }

    function renderSegments() {
        var counts = segmentCounts(state.attachments);
        ['incoming', 'sent', 'all'].forEach(function (seg) {
            var count = getEl('filesCount_' + seg);
            if (count) count.textContent = String(counts[seg]);
            var btn = getEl('filesSeg_' + seg);
            if (btn) btn.setAttribute('aria-pressed', state.segment === seg ? 'true' : 'false');
        });
    }

    function isPhone() {
        try { return Boolean(window.matchMedia && window.matchMedia('(max-width: 768px)').matches); } catch (e) { return false; }
    }

    function setFlag(el, cls, on) {
        if (el && el.classList && el.classList.toggle) el.classList.toggle(cls, Boolean(on));
    }

    // Phone toolbar: [segments] [search] [more]; the search field takes the toolbar's place (like the gallery filter), the more
    // menu holds Select, Refresh and the storage line. Elsewhere the same elements are laid out as a normal toolbar.
    function renderToolbar() {
        var bar = getEl('filesToolbar');
        setFlag(bar, 'is-search', state.searchOpen);
        var menu = getEl('filesMoreMenu');
        if (menu) { if (menu.hidden !== undefined) menu.hidden = !state.moreOpen; else menu.style.display = state.moreOpen ? '' : 'none'; }
        var more = getEl('filesMoreBtn');
        if (more) more.setAttribute('aria-expanded', state.moreOpen ? 'true' : 'false');
        setFlag(getEl('filesView'), 'is-selecting', state.selectMode);
        var count = getEl('filesSearchCount');
        if (count) {
            var q = state.search.trim();
            if (!q) { count.textContent = ''; } else {
                var total = state.attachments.filter(function (a) { return state.segment === 'all' || segmentOf(a) === state.segment; }).length;
                count.textContent = tparams('files.search_count', { shown: visibleAttachments().length, total: total },
                    visibleAttachments().length + ' of ' + total);
            }
        }
        var search = getEl('filesSearch');
        if (search && search.value !== state.search) search.value = state.search;
    }

    function closeMoreMenu() {
        if (!state.moreOpen) return;
        state.moreOpen = false;
        renderToolbar();
    }

    function setSearchOpen(on) {
        state.searchOpen = Boolean(on);
        if (!state.searchOpen && state.search) { state.search = ''; renderTransfers(); } else renderToolbar();
        var input = getEl('filesSearch');
        if (state.searchOpen && input && input.focus) { try { input.focus(); } catch (e) { /* no focus in a stub */ } }
    }

    function renderTransfers() {
        var list = getEl('filesArchiveList');
        if (!list) return;
        renderSegments();
        renderToolbar();
        var items = visibleAttachments();
        var chat = Boolean(state.counterparty);
        if (list.classList) {
            list.classList.toggle('is-chat', chat);
            list.classList.toggle('is-selecting', state.selectMode);
        }
        if (!items.length) {
            list.innerHTML = '<div class="files-empty">' + esc(t('files.no_transfers', 'No files here yet.')) + '</div>';
            renderBulkBar();
            return;
        }
        var html = groupByDay(items, chat).map(function (g) {
            return '<div class="files-day">' + esc(g.label) + '</div>' + g.items.map(cardHtml).join('');
        }).join('');
        // a poll must not throw away the scroll position the user is looking at
        var top = list.scrollTop;
        list.innerHTML = html;
        if (chat && state.chatScrolledFor !== state.counterparty) {
            state.chatScrolledFor = state.counterparty;
            list.scrollTop = list.scrollHeight;
        } else if (!chat) {
            state.chatScrolledFor = '';
            list.scrollTop = top;
        } else {
            list.scrollTop = top;
        }
        renderBulkBar();
        if (state.focusId) { var f = state.focusId; state.focusId = ''; focusCard(f); }
    }

    function focusCard(id) {
        var el = getEl('filesCard-' + id);
        if (!el) return;
        if (el.scrollIntoView) el.scrollIntoView({ block: 'center' });
        if (el.classList && el.classList.add) { el.classList.add('is-focus'); }
    }

    // MCA-UX1: a chat bubble opens its transfer. The list is one column, so the card is brought into view and flashes.
    function selectTransferFromOutside(id) {
        id = String(id || '');
        if (!id) return;
        var found = state.attachments.filter(function (a) { return a.id === id; })[0];
        if (found) {
            if (state.segment !== 'all' && segmentOf(found) !== state.segment) setSegment('all');
            state.focusId = id;
            renderTransfers();
            return;
        }
        state.pendingSelectId = id;
        if (state.active) refresh();
    }

    function setSegment(seg) {
        if (seg !== 'incoming' && seg !== 'sent' && seg !== 'all') return;
        state.segment = seg;
        writeSegment(seg);
        renderTransfers();
    }

    // ---- multi-select ------------------------------------------------------------------------------------------------
    function checkedIds() { return Object.keys(state.checked).filter(function (id) { return state.checked[id]; }); }

    function renderBulkBar() {
        var bar = getEl('filesBulkBar');
        var btn = getEl('filesSelectBtn');
        if (btn) btn.setAttribute('aria-pressed', state.selectMode ? 'true' : 'false');
        if (!bar) return;
        var n = checkedIds().length;
        if (bar.hidden !== undefined) bar.hidden = !state.selectMode; else bar.style.display = state.selectMode ? '' : 'none';
        var count = getEl('filesBulkCount');
        if (count) count.textContent = plural('files.bulk_count', n, '{count} selected', '{count} selected');
    }

    function toggleCard(id) {
        if (state.openCard[id]) delete state.openCard[id]; else state.openCard[id] = true;
        var card = getEl('filesCard-' + id);
        setFlag(card, 'is-open', state.openCard[id]);
        var chev = card && card.querySelector ? card.querySelector('.files-card-chev') : null;
        if (chev) chev.setAttribute('aria-expanded', state.openCard[id] ? 'true' : 'false');
    }

    function setSelectMode(on) {
        state.selectMode = Boolean(on);
        if (!state.selectMode) state.checked = {};
        renderTransfers();
    }

    function toggleChecked(id, force) {
        var on = force === undefined ? !state.checked[id] : Boolean(force);
        if (on) state.checked[id] = true; else delete state.checked[id];
        var card = getEl('filesCard-' + id);
        if (card && card.classList && card.classList.toggle) card.classList.toggle('is-checked', on);
        renderBulkBar();
    }

    function checkedAttachments() {
        var ids = checkedIds();
        return state.attachments.filter(function (a) { return ids.indexOf(a.id) !== -1; });
    }

    function bulkDownload() {
        var items = checkedAttachments().filter(function (a) { return a.content_available && !a.locked; });
        if (!items.length) { toast(t('files.bulk_nothing_download', 'Nothing to download in the selection'), 'info'); return; }
        var risky = items.some(function (a) { return isExecutableName(a.file_name); });
        var go = function () {
            items.forEach(function (a, i) { setTimeout(function () { downloadToDevice(a.id); }, i * 400); });
            setSelectMode(false);
        };
        if (!risky) { go(); return; }
        confirmExecutable().then(function (yes) { if (yes) go(); });
    }

    function bulkDelete() {
        var items = checkedAttachments().filter(function (a) { return a.direction !== 'sent' && a.state === 'AVAILABLE' && a.content_available && !a.locked; });
        if (!items.length) { toast(t('files.bulk_nothing_delete', 'Only received files can be deleted'), 'info'); return; }
        var dialog = (typeof window !== 'undefined') ? window.MCDialog : null;
        var asked = (dialog && dialog.confirm)
            ? dialog.confirm({
                title: t('files.confirm_bulk_delete_title', 'Delete the selected files?'),
                message: plural('files.confirm_bulk_delete_body', items.length,
                    'Delete {count} file from this node? The entry stays in the history.', 'Delete {count} files from this node? The entries stay in the history.'),
                confirmLabel: t('files.delete', 'Delete'), cancelLabel: t('dialog.cancel', 'Cancel'), danger: true,
            })
            : Promise.resolve(false);
        asked.then(function (yes) {
            if (!yes) return;
            items.reduce(function (chain, a) { return chain.then(function () { return deleteLocalRequest(a.id); }); }, Promise.resolve())
                .then(function () { setSelectMode(false); refreshTransfers(); });
        });
    }

    // ---- the line under the list -------------------------------------------------------------------------------------
    function filesBytes() {
        return state.attachments.reduce(function (sum, a) { return sum + (a.content_available ? (a.plain_size || 0) : 0); }, 0);
    }

    function renderFooter() {
        var el = getEl('filesFooter');
        var use = getEl('filesMenuUse');
        if (use) use.textContent = tparams('files.footer_use', { size: fmtBytes(filesBytes()) }, 'Files use ' + fmtBytes(filesBytes()));
        if (!el) return;
        if (!state.attachments.length && !state.storage) { el.innerHTML = ''; return; }
        el.innerHTML =
            '<span class="files-footer-text">' + esc(tparams('files.footer_use', { size: fmtBytes(filesBytes()) }, 'Files use ' + fmtBytes(filesBytes()))) + '</span>' +
            '<button type="button" class="files-footer-btn" data-files-action="purge-received">' + esc(t('files.clean_up', 'Clean up...')) + '</button>';
    }

    function loadStorage() {
        return api('/api/attachments/received-cache').then(function (r) {
            state.storage = (r.status === 200 && r.data && r.data.ok) ? { count: r.data.count || 0, bytes: r.data.bytes || 0 } : null;
            renderFooter();
        }).catch(function () { /* the line stays as it was */ });
    }

    // ---- MCA-UX1: the Files tab dot ------------------------------------------------------------------------------
    // Counts transfers that need attention and appeared after Files was last looked at; hidden while Files is shown.
    var BADGE_POLL_MS = 60000;
    var SEEN_KEY = 'mc.files.seenAt';
    var badgeTimer = null;

    function readSeenAt() {
        try { return Number(window.localStorage.getItem(SEEN_KEY)) || 0; } catch (e) { return 0; }
    }

    function writeSeenAt(value) {
        try { window.localStorage.setItem(SEEN_KEY, String(value)); } catch (e) { /* the dot just stays as it was */ }
    }

    function newestCreatedAt(list) {
        return (list || []).reduce(function (acc, a) { return Math.max(acc, Number(a && a.created_at) || 0); }, 0);
    }

    function renderBadge(count) {
        var el = getEl('mainTabFilesBadge');
        if (!el) return;
        var shown = count > 0 && !state.active;
        el.hidden = !shown;
        el.textContent = shown ? ((window.MCViews && window.MCViews.badgeLabel) ? window.MCViews.badgeLabel(count) : String(count)) : '';
        var tab = getEl('mainTabFiles');
        if (tab) {
            if (shown) tab.setAttribute('aria-label', tparams('files.badge_aria', { count: count }, 'Files, needs attention: ' + count));
            else tab.removeAttribute('aria-label');
        }
    }

    // While Files is shown, everything on screen counts as seen
    function noteFilesSeen() {
        if (!state.active) return;
        var newest = newestCreatedAt(state.attachments);
        if (newest > readSeenAt()) writeSeenAt(newest);
        renderBadge(0);
    }

    function refreshBadge() {
        if (state.active) { renderBadge(0); return Promise.resolve(); }
        return api('/api/attachments?limit=200').then(function (r) {
            if (r.status !== 200 || !r.data || r.data.ok !== true) return;
            var list = Array.isArray(r.data.attachments) ? r.data.attachments : [];
            if (!readSeenAt()) writeSeenAt(newestCreatedAt(list));   // first run on this device: what is there is not "new"
            var count = (window.MCViews && window.MCViews.newAttentionCount)
                ? window.MCViews.newAttentionCount(list, isAttention, readSeenAt()) : 0;
            renderBadge(count);
        }).catch(function () { /* keep the dot as it is */ });
    }

    function startBadge() {
        if (badgeTimer || typeof window === 'undefined') return;
        refreshBadge();
        badgeTimer = setInterval(refreshBadge, BADGE_POLL_MS);
        if (typeof document !== 'undefined' && document.addEventListener) {
            document.addEventListener('visibilitychange', function () { if (!document.hidden) refreshBadge(); });
        }
    }

    // ---- MCA-UX1: the space the received copies take, and deleting the old ones ---------------------------------

    function purgeReceivedCopies() {
        var dialog = (typeof window !== 'undefined') ? window.MCDialog : null;
        if (!dialog || !dialog.prompt || !dialog.confirm) return Promise.resolve();
        return dialog.prompt({
            title: t('files.purge_title', 'Delete older received copies'),
            message: t('files.purge_message', 'Received files older than this many days are deleted from this node. Files saved with Save to Files are kept.'),
            fields: [{
                name: 'days', label: t('files.purge_days_label', 'Older than (days)'), value: '30', inputmode: 'numeric',
                validate: function (value) {
                    var n = Number(String(value).trim());
                    return Number.isInteger(n) && n >= 1 && n <= 3650 ? '' : t('files.purge_days_invalid', 'Enter a whole number from 1 to 3650');
                },
            }],
        }).then(function (values) {
            if (!values) return null;
            var days = Number(String(values.days).trim());
            return api('/api/attachments/received-cache?older_than_days=' + days).then(function (r) {
                if (r.status !== 200 || !r.data || !r.data.ok) { toast(filesErrorCode(r.data), 'error'); return null; }
                if (!r.data.older_count) {
                    toast(tparams('files.purge_none', { days: days }, 'No received copies older than ' + days + ' days.'), 'info');
                    return null;
                }
                return dialog.confirm({
                    title: t('files.purge_confirm_title', 'Delete received copies?'),
                    message: tparams('files.purge_confirm', { count: r.data.older_count, size: fmtBytes(r.data.older_bytes), days: days },
                        'Delete ' + r.data.older_count + ' received copies (' + fmtBytes(r.data.older_bytes) + ') older than ' + days + ' days? This cannot be undone.'),
                    confirmLabel: t('files.purge_confirm_button', 'Delete'),
                    cancelLabel: t('dialog.cancel', 'Cancel'),
                    danger: true,
                }).then(function (ok) {
                    if (!ok) return null;
                    return api('/api/attachments/received-cache/purge', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ older_than_days: days }),
                    }).then(function (p) {
                        if (p.status === 202 && p.data && p.data.command_id) {
                            trackCommand(p.data.command_id, {
                                resourceKey: 'purge:received',
                                queued: t('files.purge_queued', 'Deleting received copies...'),
                                success: t('files.purge_done', 'Received copies deleted'),
                                onSuccess: function () { return Promise.all([loadStorage(), refreshTransfers()]); },
                                onUnknown: function () { return loadStorage(); },
                            });
                        } else if (p.status === 429) {
                            toast(t('files.error.command_queue_full', 'Too many pending actions - try again in a moment'), 'warning');
                        } else {
                            toast(filesErrorCode(p.data), 'error');
                        }
                    });
                });
            });
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }


    function actionBtn(id, action, label, danger) {
        var busy = state.busy['attach:' + id];
        var disabled = busy ? ' disabled' : '';
        return '<button type="button" class="files-action-btn' + (danger ? ' is-danger' : '') + '"' +
            ' data-files-action="' + esc(action) + '" data-attachment="' + esc(id) + '"' + disabled + '>' +
            esc(label) + '</button>';
    }

    function detailRowsHtml(rows) {
        return rows.map(function (r) {
            return '<div class="files-detail-row"><span class="files-detail-label">' +
                esc(t(r[0], r[1])) + '</span><span class="files-detail-value">' + esc(r[2]) + '</span></div>';
        }).join('');
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
                    onSuccess: function () { return refreshTransfers(); },
                    onUnknown: function () { return refreshTransfers(); },
                });
            } else if (r.status === 409) {
                toast(t('files.state_changed', 'This transfer changed - refreshing'), 'info');
                loadTransfers();
            } else {
                toast(filesErrorCode(r.data), 'error');
            }
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    // "Accept and download" is itself the explicit decision: no second confirmation (MCA-UX2)
    function attachmentDownload(id) {
        attachmentCommand(id, 'download', {
            queued: t('files.download_started', 'Download started'),
            success: t('files.downloaded', 'Downloaded'),
        });
    }
    // Reject cannot be undone: one danger confirmation (the shared UI1 dialog; the Files modal where it is missing)
    function attachmentReject(id) {
        var dialog = (typeof window !== 'undefined') ? window.MCDialog : null;
        var asked = (dialog && dialog.confirm)
            ? dialog.confirm({
                title: t('files.confirm_reject_title', 'Reject this transfer?'),
                message: t('files.confirm_reject_body', 'The sender is told that you declined. This cannot be undone.'),
                confirmLabel: t('files.reject', 'Reject'),
                cancelLabel: t('dialog.cancel', 'Cancel'),
                danger: true,
            })
            : confirmDialog(confirmSpec('files.confirm_reject_title', 'files.confirm_reject_body', 'files.reject',
                'Reject this transfer?', 'The sender is told that you declined. This cannot be undone.'));
        asked.then(function (yes) {
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
    // One request to delete the local copy; resolves when the command is accepted (the bulk delete chains these)
    function deleteLocalRequest(id) {
        var resourceKey = 'attach:' + id;
        if (state.busy[resourceKey]) return Promise.resolve();
        return api('/api/attachments/' + encodeURIComponent(id) + '/local-content', { method: 'DELETE' }).then(function (r) {
            if (r.status === 202 && r.data && r.data.command_id) {
                trackCommand(r.data.command_id, {
                    resourceKey: resourceKey,
                    queued: t('files.deleting_local', 'Deleting...'),
                    success: t('files.local_deleted', 'File deleted'),
                    onSuccess: function () { return refreshTransfers(); },
                    onUnknown: function () { return refreshTransfers(); },
                });
            } else {
                toast(filesErrorCode(r.data), 'error');
            }
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    function attachmentDeleteLocal(id) {
        confirmDialog(confirmSpec('files.confirm_delete_local_title', 'files.confirm_delete_local_body', 'files.delete',
            'Delete this file?', 'The file is removed from this node. The entry stays in the history.')).then(function (yes) {
            if (yes) deleteLocalRequest(id);
        });
    }

    // "Download to device" - a same-origin link to /content with a download attribute; must never call /save (C6).
    function downloadToDevice(id) {
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

    function confirmExecutable() {
        var dialog = (typeof window !== 'undefined') ? window.MCDialog : null;
        var spec = {
            title: t('files.exec_title', 'Executable file'),
            message: t('files.exec_body', 'This is an executable file. Open it only if you trust the sender.'),
            confirmLabel: t('files.exec_confirm', 'Download anyway'),
            cancelLabel: t('dialog.cancel', 'Cancel'),
            danger: true,
        };
        if (dialog && dialog.confirm) return dialog.confirm(spec);
        return confirmDialog(confirmSpec('files.exec_title', 'files.exec_body', 'files.exec_confirm', spec.title, spec.message));
    }

    function attachmentDownloadToDevice(id) {
        var a = state.attachments.filter(function (x) { return x.id === id; })[0];
        if (a && isExecutableName(a.file_name)) {
            confirmExecutable().then(function (yes) { if (yes) downloadToDevice(id); });
            return;
        }
        downloadToDevice(id);
    }

    // "Open preview" - inline image preview in a new tab.
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
                    onSuccess: function () { return refreshContactsAfterCommand(); },
                    onUnknown: function () { return refreshContactsAfterCommand(); },
                });
            } else if (r.status === 429) {
                var retry = (r.data && r.data.retry_after_seconds) || 600;
                toast(tparams('files.rate_limited', { seconds: retry }, 'Rate-limited - try again in ' + retry + 's'), 'error');
            } else if (r.status === 409) {
                toast(t('files.state_changed', 'This contact changed - refreshing'), 'info');
                refreshContacts();
            } else {
                toast(filesErrorCode(r.data), 'error');
            }
        }).catch(function () {
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    // Step 4: the single confirmation screen for the whole "establish a
    // secure connection" step, shared by every entry point (sidebar node
    // card AND the Send dialog's own banner) - one implementation, one
    // screen, plain-language copy explaining WHY rather than naming the
    // wire protocol. Deliberately still an explicit confirm (a real network
    // action), not a silent auto-fire; what changed for Step 4 is that the
    // USER no longer has to go find and trigger this separately before
    // sending - the Send dialog surfaces it automatically for the selected
    // recipient. The reply-trust screen (contactConfirm) and the key-change
    // screens below stay untouched and manual, as required.
    function contactRequestKey(contactId) {
        var c = findContact(contactId);
        var name = c && c.name ? c.name : contactId;
        // confirmDialog() replaces whatever modal is currently open with its
        // own - if this was triggered from the Send dialog's banner, that
        // dialog is gone the instant the confirm prompt appears. Remember to
        // restore it once the user has answered, whichever way, so "Cancel"
        // and "Continue" both land the user back where they were rather than
        // dropping them with no dialog at all.
        var reopenSend = isSendDialogOpen();
        confirmDialog({
            title: t('files.request_key_title', 'Set up a secure connection'),
            bodyText: tparams('files.request_key_body', { name: name },
                'A secure, encrypted connection is being set up with ' + name + ' - this happens automatically and only once. Once it is done, you can send files to them normally.'),
            confirmLabel: t('files.request_key_confirm', 'Continue'),
            danger: false,
        }).then(function (yes) {
            if (reopenSend) openSendDialog();
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
                success: t('files.key_change_accepted', 'Key change accepted - confirm the new key next'),
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

    // ---- confirmations (C9) - one reusable accessible component -------------

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
            return ''; // ambiguous body source - fail closed
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
        var wasSend = isSendDialogOpen();
        var el = state.dialog;
        state.dialog = null;
        if (el._filesOnResolve) el._filesOnResolve(result);
        if (el.parentNode) el.parentNode.removeChild(el);
        restoreBackgroundInert();
        if (state.dialogReturnFocus && typeof state.dialogReturnFocus.focus === 'function') {
            try { state.dialogReturnFocus.focus(); } catch (_) { /* ignore */ }
        }
        state.dialogReturnFocus = null;
        // G2: closing a Send dialog ends its generation, so any provider/
        // connectivity/settings/readiness response still in flight for it is
        // dropped rather than mutating the next dialog.
        if (wasSend) invalidateSendGeneration();
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
            // F4: store the prior native inert state (a real Element.inert boolean
            // in the browser, an observable expando on the test double) alongside
            // the prior aria-hidden, so BOTH are restored on every close path.
            if (typeof c._filesPrevInert === 'undefined') {
                c._filesPrevInert = (c.inert === true);
            }
            if (typeof c._filesPrevAriaHidden === 'undefined') {
                c._filesPrevAriaHidden = c.getAttribute ? c.getAttribute('aria-hidden') : null;
            }
            if (c.setAttribute) c.setAttribute('aria-hidden', 'true');
            c.inert = true; // native property in the browser; expando in the harness
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
            if (typeof c._filesPrevInert !== 'undefined') {
                c.inert = c._filesPrevInert === true;
                delete c._filesPrevInert;
            }
        }
    }

    function onDocumentKeydown(e) {
        // The Send dialog's controls are native <button>/<select> elements, so
        // Enter/Space reach them natively as a click. No custom key handling
        // here - only the modal-level Escape dismiss is intercepted.
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

    // ---- event delegation (C10) - one document-level click listener --------

    function closestAttr(el, attr) {
        var node = el;
        while (node && typeof node.getAttribute === 'function') {
            if (node.getAttribute && node.getAttribute(attr) !== null && node.getAttribute(attr) !== undefined) return node;
            node = node.parentNode;
        }
        return null;
    }

    function closestSel(el, selector) {
        while (el && el.nodeType === 1) {
            if (el.matches && el.matches(selector)) return el;
            el = el.parentNode;
        }
        return null;
    }

    function onDocumentClick(e) {
        var target = e.target || e.srcElement;
        if (!target) return;

        if (state.moreOpen && !closestSel(target, '#filesMoreMenu') && !closestSel(target, '#filesMoreBtn')) {
            state.moreOpen = false;
            renderToolbar();
        }

        // FX1: while selecting, a tap on the card itself ticks it (buttons, links and the details toggle keep their own job)
        if (state.selectMode) {
            var card = closestSel(target, '.files-card');
            if (card && !closestSel(target, 'button, a, summary, input, .files-card-comment')) {
                if (state.longPressed) { state.longPressed = false; return; }
                toggleChecked(card.getAttribute('data-attachment'));
                var box = card.querySelector ? card.querySelector('input[data-files-check]') : null;
                if (box) box.checked = Boolean(state.checked[card.getAttribute('data-attachment')]);
                return;
            }
        }

        // phone: a tap on the card (not on a button, link, the details toggle or the comment) opens or closes it
        if (!state.selectMode && isPhone()) {
            var tapped = closestSel(target, '.files-card');
            if (tapped && !closestSel(target, 'button, a, summary, input, details, .files-card-comment')) {
                if (state.longPressed) { state.longPressed = false; return; }
                toggleCard(tapped.getAttribute('data-attachment'));
                return;
            }
        }

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
        if (action === 'relay-wizard') { openRelayWizard(); return; }
        if (action === 'refresh') { state.moreOpen = false; renderToolbar(); refresh(); return; }
        if (action === 'segment') { setSegment(modal.getAttribute('data-segment')); return; }
        if (action === 'select-mode') { state.moreOpen = false; setSelectMode(!state.selectMode); return; }
        if (action === 'search-open') { state.moreOpen = false; setSearchOpen(true); return; }
        if (action === 'search-close') { setSearchOpen(false); return; }
        if (action === 'more-toggle') { state.moreOpen = !state.moreOpen; renderToolbar(); return; }
        if (action === 'select-mode-off') { setSelectMode(false); return; }
        if (action === 'bulk-download') { bulkDownload(); return; }
        if (action === 'bulk-delete') { bulkDelete(); return; }
        if (action === 'purge-received') { state.moreOpen = false; renderToolbar(); purgeReceivedCopies(); return; }

        // Attachment + contact + provider actions carry a data-attachment /
        // data-contact / data-provider id validated against the current model.
        var attachmentId = modal.getAttribute('data-attachment');
        var contactId = modal.getAttribute('data-contact');
        var providerId = modal.getAttribute('data-provider');

        if (attachmentId && state.attachments.some(function (a) { return a.id === attachmentId; })) {
            if (action === 'toggle-card') { toggleCard(attachmentId); return; }
            if (action === 'toggle-comment') { state.openComment[attachmentId] = !state.openComment[attachmentId]; renderTransfers(); return; }
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

        // PR 5: the MCA key actions now live in the sidebar node card (chat.js),
        // not a Files contact list. They route through this delegated handler via
        // `data-files-action` + `data-contact`; the guard is presence of a
        // canonical `data-contact` only (the action itself resolves the target
        // via findContact, which falls back to the shared store). The former
        // `contact-filter` action is gone - selection is now exclusively the
        // store's toggleSelect, driven by the node-card select button.
        if (contactId) {
            if (action === 'contact-request-key') { contactRequestKey(contactId); return; }
            if (action === 'contact-cancel-key') {
                contactCommand(contactId, 'cancel-key-request', {
                    queued: t('files.cancelling_key_request', 'Stopping key requests…'),
                    success: t('files.key_request_cancelled', 'Key requests stopped'),
                });
                return;
            }
            if (action === 'contact-confirm') { contactConfirm(contactId); return; }
            if (action === 'contact-accept') { contactAcceptKeyChange(contactId); return; }
            if (action === 'contact-reject') { contactRejectKeyChange(contactId); return; }
            // Step 4: the Send dialog's own "Connect securely" banner button -
            // same single confirmation screen as the sidebar node card's
            // "Request key" action (contactRequestKey is the one implementation
            // both entry points share).
            if (action === 'send-request-key') { contactRequestKey(contactId); return; }
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

        // Relay setup wizard.
        if (action === 'wizard-probe') { relayWizardProbe(); return; }
        if (action === 'wizard-back-url') { renderRelayWizardStep('url'); return; }
        if (action === 'wizard-confirm') { relayWizardConfirm(); return; }
        if (action === 'wizard-skip-token') { renderRelayWizardStep('readiness'); return; }
        if (action === 'wizard-save-token') { relayWizardSaveToken(); return; }
        if (action === 'wizard-check-readiness') { renderRelayWizardStep('readiness'); return; }
        if (action === 'wizard-advanced') { openProviderSettings(); return; }
        if (action === 'wizard-finish') { closeModal(false); return; }
    }

    function onDocumentInput(e) {
        var target = e.target;
        if (!target || !target.id) return;
        if (target.id === 'filesSearch') {
            state.search = target.value || '';
            renderTransfers();
            return;
        }
        // The custom-TTL number input fires 'input' as digits are typed, so the
        // expiry summary updates live rather than only on blur; the authoritative
        // readiness refresh is debounced (F2).
        if (target.id === 'filesSendComment') { renderSendCommentLeft(); renderSendSubmit(); return; }
        if (target.id === 'filesSendTtlValue') {
            renderSendExpiry();
            renderSendSubmit();
            scheduleSendReadinessRefresh();
            return;
        }
    }

    // 'change' fires for <select> and <input type=file>, which do not emit a
    // useful 'input' event; this routes the send form's dynamic feedback.
    function onDocumentChange(e) {
        var target = e.target;
        if (target && target.getAttribute && target.getAttribute('data-files-setting')) { onFilesSettingChange(target); return; }
        if (target && target.getAttribute && target.getAttribute('data-files-check')) { toggleChecked(target.getAttribute('data-files-check'), Boolean(target.checked)); return; }
        if (!target || !target.id) return;
        if (target.id === 'filesSendExpiry') { renderSendCustomTtl(); renderSendSubmit(); refreshSendReadiness(); return; }
        if (target.id === 'filesSendTtlUnit') { renderSendExpiry(); renderSendSubmit(); refreshSendReadiness(); return; }
        if (target.id === 'filesSendFile') { renderSendFileFeedback(); return; }
        if (target.id === 'filesSendProvider') { renderSendExpiryOptions(); renderSendProviderReadiness(); refreshSendReadiness(); return; }
        if (target.id === 'filesSendRecipient') { renderSendKeyStatus(); renderSendSubmit(); return; }
        if (target.id === 'filesSendResize') { renderSendSubmit(); return; }
    }

    // ---- filter (C5) -------------------------------------------------------

    // PR 4 Finding 1: the shared store's selection is the single source of
    // truth for the Files counterparty filter. A node selection becomes the
    // counterparty; a channel selection (or no selection) clears it - a channel
    // stays selected in the store but is never a file counterparty.
    function onStoreSelection(evt) {
        var sel = evt && evt.selection;
        var nodeId = (sel && sel.kind === 'node') ? canonicalNodeId(sel.id) : '';
        var changed = state.counterparty !== nodeId;
        state.counterparty = nodeId;
        if (!state.active) return; // re-derived on the next activate()→refresh()
        // a channel is never a file counterparty: with a channel selected the list simply shows everything
        if (changed) loadTransfers(); // avoid a reload loop on a no-op counterparty change
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
        state.segment = readSegment();
        state.visible = !(typeof document !== 'undefined' && document.hidden);
        // PR 4 Finding 1: subscribe to the shared target store once, so the
        // counterparty filter always mirrors the store's selection (node clicks
        // in the contact list route through store.toggleSelect). The store is a
        // page singleton and files.js is always loaded, so the subscription
        // lives for the page; the onStoreSelection guard re-syncs on reactivate.
        if (!state.storeSubscribed) {
            var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
            if (store && typeof store.subscribe === 'function') {
                store.subscribe(onStoreSelection);
                state.storeSubscribed = true;
            }
        }
        refresh();
        schedule();
        loadConnectivity();
        loadSettings();
    }

    function deactivate() {
        state.active = false;
        state.epoch++;
        stopTimer();
        state.selectMode = false;
        state.checked = {};
        closeModal(false);
        // In-flight reads are dropped by the epoch bump; submitted commands are left untouched.
        refreshBadge();   // MCA-UX1: the tab dot counts again as soon as Files is left
    }

    function refresh() {
        if (!state.active) return;
        refreshContacts();
        loadStorage();
        loadTransfers();
    }

    // ---- send dialog (C7) --------------------------------------------------

    function openSendDialog(options) {
        if (typeof document === 'undefined') return;
        closeModal();
        // Gallery G2: the same dialog, with pictures from the Media gallery instead of a file picker
        var pictures = options && Array.isArray(options.pictures) && options.pictures.length ? options.pictures : null;
        state.sendPictures = pictures ? {
            items: pictures.slice(0, MAX_SEND_PICTURES),
            total: pictures.length,
            resize: '1280',
            onDone: typeof options.onDone === 'function' ? options.onDone : null,
        } : null;
        // G2: this dialog owns a fresh generation; every load/readiness it starts
        // is scoped to it and dropped the moment it closes or is replaced.
        var gen = ++state.sendGeneration;
        var id = 'files-send';
        var html =
            '<div class="files-modal-backdrop" data-files-action="modal-backdrop-close" role="presentation">' +
                '<div class="files-modal" role="dialog" aria-modal="true" aria-labelledby="' + id + '-title">' +
                    '<div class="files-modal-header">' +
                        '<h3 class="files-modal-title" id="' + id + '-title">📤 ' + esc(pictures ? t('files.pictures_title', 'Send pictures') : t('files.send_file', 'Send file')) + '</h3>' +
                        '<button type="button" class="files-modal-close" data-files-action="modal-close" aria-label="' + esc(t('files.dialog_close', 'Close dialog')) + '">×</button>' +
                    '</div>' +
                    '<div class="files-modal-body">' +
                        '<div class="files-vault-notice" id="filesSendVaultNotice" data-vault-notice hidden></div>' +
                        '<div class="files-send-status" id="filesSendStatus" aria-live="polite"></div>' +
                        '<div class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_recipient', 'Recipient')) + '</span>' +
                            // UX5: one searchable picker (static/recipient-picker.js); the hidden input keeps the chosen id
                            '<div class="rp" id="filesSendRecipientPicker"></div>' +
                            '<input type="hidden" id="filesSendRecipient" value="" />' +
                        '</div>' +
                        '<div class="files-provider-probe" id="filesSendKeyStatus" aria-live="polite"></div>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_provider', 'Relay provider')) + '</span>' +
                            '<select id="filesSendProvider"></select>' +
                            '<div class="files-send-readiness" id="filesSendReadiness" aria-live="polite"></div>' +
                            '<div class="files-send-readiness-check" id="filesSendReadinessCheck" aria-live="polite"></div>' +
                        '</label>' +
                        (pictures ? sendPicturesFieldHtml() :
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_file_label', 'File')) + '</span>' +
                            '<input type="file" id="filesSendFile" />' +
                            '<div class="files-send-file-feedback" id="filesSendFileFeedback" aria-live="polite"></div>' +
                        '</label>') +
                        '<div class="files-field">' +
                            '<label class="files-field-label" for="filesSendExpiry">' + esc(t('files.send_expiry', 'Keep the link for')) + '</label>' +
                            '<select id="filesSendExpiry"></select>' +
                            '<div class="files-ttl-custom" id="filesSendTtlCustom" hidden>' +
                                '<input type="number" id="filesSendTtlValue" min="1" step="1" inputmode="numeric" autocomplete="off" aria-label="' +
                                    esc(t('files.send_ttl_value', 'Amount')) + '" />' +
                                '<select id="filesSendTtlUnit" aria-label="' + esc(t('files.send_ttl_unit', 'Unit')) + '">' +
                                    '<option value="minutes">' + esc(t('files.unit_minutes', 'minutes')) + '</option>' +
                                    '<option value="hours">' + esc(t('files.unit_hours', 'hours')) + '</option>' +
                                    '<option value="days" selected>' + esc(t('files.unit_days', 'days')) + '</option>' +
                                '</select>' +
                            '</div>' +
                            '<div class="files-send-hint" id="filesSendTtlHint"></div>' +
                            '<div class="files-send-expiry-summary" id="filesSendExpirySummary" aria-live="polite"></div>' +
                        '</div>' +
                        '<label class="files-field">' +
                            '<span class="files-field-label">' + esc(t('files.send_comment', 'Comment for the recipient (optional)')) + '</span>' +
                            '<input type="text" id="filesSendComment" maxlength="280" autocomplete="off" />' +
                            '<div class="files-send-hint">' + esc(t('files.send_comment_hint', 'Sent encrypted together with the file, not as a chat message')) + '</div>' +
                            '<div class="files-send-hint" id="filesSendCommentLeft" aria-live="polite"></div>' +
                        '</label>' +
                    '</div>' +
                    '<div class="files-modal-footer">' +
                        '<button type="button" class="files-modal-cancel" data-files-action="send-cancel">' + esc(t('common.cancel', 'Cancel')) + '</button>' +
                        '<button type="button" class="files-modal-submit" data-files-action="send-submit" id="filesSendSubmit">' + esc(t('files.send_submit', 'Send')) + '</button>' +
                    '</div>' +
                '</div>' +
            '</div>';
        var el = openModalHtml(html, function () { state.sendInFlight = false; state.sendPictures = null; state.sendPicker = null; state.sendPickerHost = null; });
        el.className = 'files-dialog-root is-send';
        // G2.1/G2.3: never render the cached provider list as actionable, and
        // never check readiness against it, before the fresh provider projection
        // for THIS dialog arrives - that fresh load owns the select and readiness.
        // Render recipients/status/ttl first, then start the generation-scoped
        // loads; each settles and updates the open dialog independently.
        renderSendRecipients();
        renderSendKeyStatus();
        renderSendProvidersLoading();
        renderSendStatus();
        renderSendExpiryOptions();
        renderSendCommentLeft();
        renderSendSubmit();
        loadConnectivityForSend(gen);
        loadSettingsForSend(gen);
        loadProvidersForSend(gen);
    }

    // ---- gallery G2: pictures instead of a file ------------------------------

    function sendPicturesFieldHtml() {
        var pics = state.sendPictures;
        var summary = tparams('files.pictures_count', { count: pics.items.length }, 'Selected: ' + pics.items.length);
        var capped = pics.total > pics.items.length
            ? '<div class="files-send-file-feedback">' + esc(tparams('files.pictures_max', { max: MAX_SEND_PICTURES }, 'At most ' + MAX_SEND_PICTURES + ' pictures per send.')) + '</div>' : '';
        return '<div class="files-field">' +
            '<span class="files-field-label">' + esc(t('files.pictures_label', 'Pictures')) + '</span>' +
            '<div class="files-send-pictures-summary" id="filesSendPictures">' + esc(summary) + '</div>' + capped +
            '</div>' +
            '<label class="files-field">' +
                '<span class="files-field-label">' + esc(t('files.pictures_resize', 'Resize before sending')) + '</span>' +
                '<select id="filesSendResize">' +
                    '<option value="1280" selected>' + esc(t('files.pictures_resize_1280', 'Small, 1280 px (recommended)')) + '</option>' +
                    '<option value="1920">' + esc(t('files.pictures_resize_1920', 'Medium, 1920 px')) + '</option>' +
                    '<option value="original">' + esc(t('files.pictures_resize_original', 'Original (up to 5 MB each)')) + '</option>' +
                '</select>' +
                '<div class="files-send-file-feedback" id="filesSendFileFeedback" aria-live="polite">' +
                    esc(t('files.pictures_metadata_note', 'Location and camera data are removed from the copies.')) + '</div>' +
            '</label>';
    }

    // the contact picked in the recipient select (pictures mode needs it before it is sendable, to offer Connect securely)
    function sendSelectedContact() {
        var sel = getEl('filesSendRecipient');
        return sel && sel.value ? findContact(sel.value) : null;
    }

    function picturesBlobName(item) {
        var name = String(item.display_name || String(item.filename || '').split('/').pop() || 'picture.jpg');
        return /\.jpe?g$/i.test(name) ? name : name + '.jpg';
    }

    // One prepared copy (resized by the server, metadata removed). {ok, blob, name} or {ok: false, message}
    function fetchPictureBlob(item, resize) {
        var path = String(item.filename || '').split('/').map(encodeURIComponent).join('/');
        return fetch('/api/camera/screenshot/' + path + '?send=' + encodeURIComponent(resize)).then(function (r) {
            if (r.ok) return r.blob().then(function (blob) { return { ok: true, blob: blob, name: picturesBlobName(item) }; });
            return r.json().catch(function () { return null; }).then(function (data) {
                if (data && data.error_code === 'too_large') {
                    return { ok: false, message: tparams('files.pictures_err_too_large', { size: fmtBytes(data.size_bytes), max: fmtBytes(data.max_bytes) },
                        'A picture ({size}) is larger than the {max} limit of a transfer. Choose a smaller size.') };
                }
                return { ok: false, message: (data && data.error) || t('files.error.network_error', 'Network error') };
            });
        }, function () { return { ok: false, message: t('files.error.network_error', 'Network error') }; });
    }

    // One transfer per picture through the ordinary POST /api/attachments (a client_request_id each), one after the other.
    // A full command queue (429) stops the run: the rest were not sent.
    function submitSendPictures(recipient, provider) {
        var pics = state.sendPictures;
        var contact = findContact(recipient.value);
        if (!contact || contact.can_send_file !== true) {
            toast(t('files.pictures_err_not_trusted', 'Set up a secure connection with this contact first'), 'error');
            return;
        }
        var ttl = sendTtlSecondsStrict();
        if (ttl === null) {
            toast(t('files.err_ttl_custom', 'Enter a whole number greater than zero'), 'error');
            return;
        }
        var grace = sendGraceSeconds(ttl);
        var resizeEl = getEl('filesSendResize');
        var resize = resizeEl && resizeEl.value ? resizeEl.value : '1280';
        var commentEl = getEl('filesSendComment');
        var comment = commentEl && commentEl.value.trim() ? commentEl.value.trim() : null;
        var items = pics.items.slice();
        var onDone = pics.onDone;
        var usedRecipient = recipient.value;
        var submit = getEl('filesSendSubmit');
        state.sendInFlight = true;
        if (submit) submit.disabled = true;
        var result = { queued: 0, failed: 0, total: items.length, firstError: '', stopped: false };
        function note(message) { if (!result.firstError) result.firstError = message; }

        function step(i) {
            if (i >= items.length || result.stopped) return Promise.resolve();
            return fetchPictureBlob(items[i], resize).then(function (got) {
                if (!got.ok) { result.failed += 1; note(got.message); return step(i + 1); }
                var form = new FormData();
                form.append('metadata', JSON.stringify({
                    client_request_id: newClientRequestId(),
                    recipient: { source_address: recipient.value },
                    hard_ttl_seconds: ttl,
                    download_grace_seconds: grace,
                    comment: comment,
                    provider_id: provider.value,
                }));
                form.append('file', got.blob, got.name);
                return api('/api/attachments', { method: 'POST', body: form }).then(function (r2) {
                    if (r2.status === 202 && r2.data && r2.data.ok && r2.data.command_id) {
                        result.queued += 1;
                    } else if (r2.status === 429) {
                        result.failed += items.length - i;
                        result.stopped = true;
                        note(t('files.pictures_err_queue_full', 'The send queue is full. Wait a moment and send the rest again.'));
                    } else {
                        result.failed += 1;
                        note(filesErrorCode(r2.data));
                    }
                    return step(i + 1);
                });
            });
        }

        api('/api/mca/providers/' + encodeURIComponent(provider.value) + '/upload-readiness?requested_ttl_seconds=' + ttl).then(function (r) {
            if (!r.data || r.data.ok !== true || !r.data.ready) {
                unlockSend();
                toast(r.data && r.data.reason ? filesReadinessLabel(r.data.reason) : t('files.err_provider_not_ready', 'Selected provider is not ready'), 'error');
                return null;
            }
            return step(0).then(function () {
                unlockSend();
                var text = tparams('files.pictures_queued', { done: result.queued, total: result.total }, result.queued + ' of ' + result.total + ' queued');
                if (result.failed) toast(text + ' ' + result.firstError, result.queued ? 'warning' : 'error');
                else toast(text, 'success');
                if (result.queued) {
                    noteRecipientUsed(usedRecipient);
                    if (onDone) { try { onDone(result); } catch (_) { /* the gallery's own business */ } }
                    return refreshTransfers().then(function () { closeModal(false); });
                }
                return null;
            });
        }).catch(function () {
            unlockSend();
            toast(t('files.error.network_error', 'Network error'), 'error');
        });
    }

    // Public entry for the Media gallery: refresh the contacts first so the recipient list is current
    function openSendPictures(pictures, onDone) {
        var ready = refreshContacts();
        return Promise.resolve(ready).catch(function () { /* the list may be stale: still usable */ }).then(function () {
            openSendDialog({ pictures: pictures, onDone: onDone });
        });
    }

    // G2.1/G2.3: an empty, explicitly-cleared provider select while the fresh
    // provider projection is still in flight. This keeps the cached provider list
    // (and any cached readiness the user could act on) out of the dialog until
    // this dialog's own provider response lands.
    function renderSendProvidersLoading() {
        var sel = getEl('filesSendProvider');
        if (!sel) return;
        sel.innerHTML = '<option value="">' + esc(t('files.loading_providers', 'Loading providers…')) + '</option>';
        sel.value = '';
        var readiness = getEl('filesSendReadiness');
        if (readiness) readiness.innerHTML = '';
    }

    function loadProvidersForSend(gen) {
        // R7: always fetch a FRESH provider projection on open - never trust the
        // cached list. G2.3: scoped to a unique per-generation key so it can
        // never be suppressed by a concurrently in-flight workspace provider load.
        return guardedLoad('providers-send-' + gen, function () { return api('/api/mca/providers'); }, function (r) {
            if (!sendGenerationCurrent(gen)) return; // a newer/closed dialog owns the form now
            state.providers = (r.status === 200 && r.data && r.data.ok && Array.isArray(r.data.providers))
                ? r.data.providers : [];
            renderSendProviders();
            renderSendStatus();
            // F2: a fresh provider projection triggers the authoritative readiness
            // request for the (now known) selected provider/TTL.
            refreshSendReadiness();
        });
    }

    function loadConnectivityForSend(gen) {
        // G2.2: a connectivity response started for an older Send dialog must
        // never update this (newer) one - scoped key + generation gate.
        return guardedLoad('connectivity-send-' + gen, function () {
            return api('/api/mca/connectivity');
        }, function (r) {
            if (!sendGenerationCurrent(gen)) return;
            if (r.status === 200 && r.data && r.data.ok) {
                state.connectivity = {
                    internet: r.data.internet || 'unknown',
                    relays: r.data.relays || {},
                };
            }
            renderSendStatus();
        });
    }

    function loadSettingsForSend(gen) {
        // G2.2: same isolation for the settings projection (transport selection).
        return guardedLoad('settings-send-' + gen, function () {
            return api('/api/settings');
        }, function (r) {
            if (!sendGenerationCurrent(gen)) return;
            if (r.status === 200 && r.data && r.data.ok && r.data.settings) {
                state.settings = r.data.settings;
            }
            renderSendStatus();
        });
    }

    function sendTrustedContacts() {
        // PR 4: sendability is the centralized capability matrix's verdict -
        // a node is a valid recipient only when `can_send_file` is true (a
        // trusted worker-published binding), never merely because it exists in
        // the node list.
        return state.contacts.filter(function (c) { return c.can_send_file === true; });
    }

    // UX5: the contacts as the picker wants them (the local node is already excluded by syncContactsFromStore)
    function recipientPickerContacts() {
        return state.contacts.map(function (c) {
            return {
                id: c.contact_id,
                short_name: c.short_name || '',
                name: c.name || '',
                favorite: c.favorite === true,
                can_send: c.can_send_file === true,
                can_request_key: c.can_request_key === true,
                status: c.status,
                reason: c.file_unavailable_reason,
                key_request_state: c.key_request_state,
            };
        });
    }

    // "Key unknown, request the key" for a node without a key; another reason (a key to confirm, a changed key) keeps its
    // own label - those are handled on the node's card, never from here
    function recipientReasonLabel(c) {
        if (c.reason === 'key_unknown' || !c.reason) {
            if (c.key_request_state === 'queued' || c.key_request_state === 'waiting_response') {
                return t('recipient.key_waiting', 'Key requested, waiting for the answer');
            }
            return t('recipient.key_unknown', 'Key unknown, request the key');
        }
        return fileUnavailableReasonLabel(c.reason);
    }

    // the picker told us the recipient: keep it in the hidden field the send form reads, then refresh what depends on it
    function onSendRecipientPicked(id) {
        var hidden = getEl('filesSendRecipient');
        if (hidden) hidden.value = id || '';
        renderSendKeyStatus();
        renderSendSubmit();
    }

    // Only a contact that can be sent to is ever chosen (a node without a key is shown, disabled, in its own group, with a
    // "Request key" action). PR 4 Finding 1 still holds: the shared store's selection pre-selects a recipient only when it is a
    // valid, still-sendable node; a channel or an invalid/disappeared node pre-selects nothing, never a different contact.
    function renderSendRecipients() {
        var host = getEl('filesSendRecipientPicker');
        var picker = (typeof window !== 'undefined') ? window.MCRecipientPicker : null;
        if (!host || !picker) return;
        var first = !state.sendPicker || state.sendPickerHost !== host;
        if (first) {
            state.sendPickerHost = host;
            state.sendPicker = picker.mount(host, {
                id: 'filesSend',
                t: function (key, params, fallback) { return params ? tparams(key, params, fallback) : t(key, fallback); },
                reasonLabel: recipientReasonLabel,
                requestKeyAttr: 'data-files-action="send-request-key"',
                onChange: onSendRecipientPicked,
            });
        }
        state.sendPicker.setContacts(recipientPickerContacts());
        if (first) {
            var preselect = sendRecipientPreselection();
            if (preselect) {
                state.sendPicker.setSelected(preselect);
                onSendRecipientPicked(state.sendPicker.getSelected());
            }
        }
    }

    // a send went to this recipient: it becomes the first of the recently used
    function noteRecipientUsed(id) {
        var picker = (typeof window !== 'undefined') ? window.MCRecipientPicker : null;
        if (picker && id) { try { picker.noteUsed(id); } catch (_) { /* just not remembered */ } }
    }

    // PR 4 Finding 1: resolve the shared store's selection to a pre-selected
    // recipient id, or `null` when a target is selected but is not a valid
    // recipient (a channel, or an invalid/disappeared/non-sendable node) - the
    // latter must never silently fall back to a different recipient.
    function sendRecipientPreselection() {
        var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
        if (!store) return '';
        var sel = store.selected();
        if (!sel) return '';                  // nothing selected → normal default
        if (sel.kind !== 'node') return null; // a channel is never a file recipient
        var c = findContact(sel.id);
        if (c && c.can_send_file === true) return c.contact_id;
        return null;                          // invalid/disappeared/non-sendable node
    }

    // Step 4: the currently store-selected node, when it is the reason the
    // recipient list can't be used yet (key_unknown) - the single, automatic
    // entry point that replaces "go find the sidebar node card and click
    // Request key". Only `key_unknown` is handled here; `confirmation_required`
    // and `key_changed` stay exclusively on the sidebar node card's explicit,
    // never-automated screens (per the task's own constraint).
    function sendKeyStatusContact() {
        if (state.sendPictures) {
            var picked = sendSelectedContact();
            return picked && picked.status === 'key_unknown' ? picked : null;
        }
        var store = (typeof window !== 'undefined') ? window.MeshCenterTargets : null;
        var sel = store && typeof store.selected === 'function' ? store.selected() : null;
        if (!sel || sel.kind !== 'node') return null;
        var c = findContact(sel.id);
        return (c && c.status === 'key_unknown') ? c : null;
    }

    function renderSendKeyStatus() {
        var el = getEl('filesSendKeyStatus');
        if (!el) return;
        var c = sendKeyStatusContact();
        if (!c) { el.innerHTML = ''; return; }
        var name = c.name || c.contact_id;
        if (c.key_request_state === 'queued' || c.key_request_state === 'waiting_response') {
            el.innerHTML = '<div class="files-provider-probe-line">' +
                esc(tparams('files.send_key_waiting', { name: name }, 'Waiting for ' + name + ' to respond…')) +
                '</div>';
            return;
        }
        if (!c.can_request_key) { el.innerHTML = ''; return; }
        el.innerHTML =
            '<div class="files-provider-probe-line">' + esc(tparams('files.send_key_banner_body', { name: name },
                'Files can only be sent once a secure connection is set up with ' + name + '.')) + '</div>' +
            '<button type="button" class="files-action-btn" data-files-action="send-request-key" data-contact="' + esc(c.contact_id) + '">' +
                esc(t('files.send_key_banner_action', 'Connect securely')) + '</button>';
    }

    // Step 4: re-render the open Send dialog's recipient list + key-status
    // banner whenever the contact projection changes underneath it (a normal
    // poll tick, or a just-completed contact command) - without this, a
    // request that gets answered while the dialog is still open would never
    // surface as "now sendable" until the user closed and reopened it.
    function refreshSendRecipientsIfOpen() {
        if (!isSendDialogOpen()) return;
        renderSendRecipients();
        renderSendKeyStatus();
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

        // R7: list ALL providers, not just the ready ones - a non-ready
        // provider is shown (disabled) with the reason, so it never silently
        // disappears and the user can see *why* it is not selectable.
        sel.innerHTML = state.providers.map(function (p) {
            var label = p.display_name || p.origin || p.provider_id;
            if (uploadReadyProvider(p)) {
                return '<option value="' + esc(p.provider_id) + '">' + esc(label) + '</option>';
            }
            return '<option value="' + esc(p.provider_id) + '" disabled>' +
                esc(label + ' - ' + providerNotReadyLabel(p)) + '</option>';
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
        renderSendExpiryOptions();       // the presets follow the relay's limits
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
        // R7: Relay state and upload readiness are rendered as SEPARATE facts -
        // the relay's reachability/identity is independent of whether uploads
        // are configured/allowed for this profile.
        // UX5: the upload readiness is stated ONCE, by the authoritative check line right under this one
        // (renderSendReadinessCheck); repeating it here printed "Upload: ready" twice
        var relay = filesRelayStateLabel(p.state);
        el.innerHTML = '<span>' + esc(t('files.relay_state_label', 'Relay state') + ': ' + relay) + '</span>';
    }

    function renderSendFileFeedback() {
        var el = getEl('filesSendFileFeedback');
        if (!el) return;
        var input = getEl('filesSendFile');
        var f = input && input.files && input.files.length ? input.files[0] : null;
        if (!f) { el.innerHTML = ''; renderSendSubmit(); return; }
        // F1: an allowlisted extension or MIME is allowed; anything else is
        // rejected locally (immediate, localized feedback). A stale rejection
        // clears the moment an allowed file is selected - the server's magic-byte
        // sniffing stays the real authority.
        if (!fileTypeAllowed(f)) {
            el.innerHTML = '<span class="files-status-error">' +
                esc(t('files.err_mime', 'File type is not allowed')) + '</span>';
            renderSendSubmit();
            return;
        }
        var name = (f.name || '').toLowerCase();
        var ext = name.indexOf('.') !== -1 ? name.split('.').pop() : '';
        var known = EXTENSION_MIME[ext] || (ALLOWED_MIME[f.type] ? f.type : '');
        var typeLine = known
            ? tparams('files.send_file_type', { type: known }, 'Type: ' + known)
            : t('files.send_file_type_unknown', 'Type: unrecognized');
        el.innerHTML =
            '<span>' + esc(typeLine) + '</span>' +
            ' · <span>' + esc(tparams('files.send_file_size', { size: fmtBytes(f.size) }, 'Size: ' + fmtBytes(f.size))) + '</span>';
        renderSendSubmit();
    }

    // ---- the expiry of a transfer (FX1): presets inside what the relay allows; Custom is a number and a unit ------------------
    var TTL_PRESETS = [3600, 21600, 86400, 259200, 604800, 1209600, 2592000];
    var TTL_PRESET_LABELS = { 3600: '1 hour', 21600: '6 hours', 86400: '1 day', 259200: '3 days', 604800: '7 days', 1209600: '14 days', 2592000: '30 days' };
    var TTL_DEFAULT = 1209600;          // 14 days
    var RELAY_MIN_TTL = 3600;           // the relay's own minimum (1 hour) until the relay publishes it in /v1/info
    var RELAY_MAX_TTL_FALLBACK = 2592000;
    var TTL_UNIT_SECONDS = { minutes: 60, hours: 3600, days: 86400 };

    function durationText(seconds) {
        if (seconds % 86400 === 0) return plural('files.dur_days', seconds / 86400, '{count} day', '{count} days');
        if (seconds % 3600 === 0) return plural('files.dur_hours', seconds / 3600, '{count} hour', '{count} hours');
        return plural('files.dur_minutes', Math.round(seconds / 60), '{count} minute', '{count} minutes');
    }

    // {min, max}: what the selected relay allows (its published minimum when it has one)
    function sendTtlLimits() {
        var sel = getEl('filesSendProvider');
        var p = sel && sel.value ? providerById(sel.value) : null;
        var min = p && p.min_ttl_seconds != null ? p.min_ttl_seconds : RELAY_MIN_TTL;
        var max = p && p.max_ttl_seconds != null ? p.max_ttl_seconds : RELAY_MAX_TTL_FALLBACK;
        return { min: min, max: max };
    }

    function ttlPresetLabel(seconds) {
        return t('files.ttl_preset_' + seconds, TTL_PRESET_LABELS[seconds]);
    }

    // The select lists only the presets this relay allows. The default is 14 days; a relay that keeps files for less has its
    // own maximum as the default, and the dialog says so.
    function renderSendExpiryOptions() {
        var sel = getEl('filesSendExpiry');
        if (!sel) return;
        var limits = sendTtlLimits();
        var allowed = TTL_PRESETS.filter(function (v) { return v >= limits.min && v <= limits.max; });
        var previous = sel.value;
        var wanted = allowed.indexOf(TTL_DEFAULT) !== -1 ? TTL_DEFAULT : (allowed.length ? allowed[allowed.length - 1] : 'custom');
        var keep = previous && (previous === 'custom' || allowed.indexOf(Number(previous)) !== -1) ? previous : String(wanted);
        sel.innerHTML = allowed.map(function (v) {
            return '<option value="' + v + '">' + esc(ttlPresetLabel(v)) + '</option>';
        }).join('') + '<option value="custom">' + esc(t('files.ttl_custom', 'Custom...')) + '</option>';
        sel.value = keep;
        var note = getEl('filesSendTtlHint');
        state.sendTtlLimited = allowed.indexOf(TTL_DEFAULT) === -1 && allowed.length > 0;
        if (note) {
            note.textContent = state.sendTtlLimited
                ? tparams('files.ttl_default_limited', { duration: durationText(wanted) }, 'This relay keeps files at most ' + durationText(wanted) + ', so that is the default.')
                : '';
        }
        renderSendCustomTtl();
    }

    function renderSendCustomTtl() {
        var sel = getEl('filesSendExpiry');
        var box = getEl('filesSendTtlCustom');
        var custom = sel && sel.value === 'custom';
        if (box) { if (box.hidden !== undefined) box.hidden = !custom; else box.style.display = custom ? '' : 'none'; }
        var hint = getEl('filesSendTtlHint');
        if (hint && custom) {
            var limits = sendTtlLimits();
            hint.textContent = tparams('files.ttl_range', { min: durationText(limits.min), max: durationText(limits.max) },
                'from ' + durationText(limits.min) + ' to ' + durationText(limits.max));
        } else if (hint && !state.sendTtlLimited) {
            hint.textContent = '';
        }
        renderSendExpiry();
    }

    // Typed in Custom: null when there is nothing valid yet. A blank field is not an error message, only no TTL.
    function customTtlSeconds() {
        var input = getEl('filesSendTtlValue');
        var unit = getEl('filesSendTtlUnit');
        var raw = input ? String(input.value || '').trim() : '';
        if (raw === '') return { seconds: null, blank: true };
        var n = parseInt(raw, 10);
        if (!Number.isFinite(n) || String(n) !== raw || n <= 0) return { seconds: null, blank: false };
        return { seconds: n * (TTL_UNIT_SECONDS[unit ? unit.value : 'days'] || 86400), blank: false };
    }

    function renderSendExpiry() {
        var summary = getEl('filesSendExpirySummary');
        if (!summary) return;
        var ttl = sendTtlSecondsStrict();
        var sel = getEl('filesSendExpiry');
        // One message, under the field, and only after something was typed (never for a blank field)
        if (ttl === null) {
            var typed = customTtlSeconds();
            summary.innerHTML = typed.blank ? '' : '<span class="files-status-warn">' +
                esc(t('files.err_ttl_custom', 'Enter a whole number greater than zero')) + '</span>';
            return;
        }
        var minMax = sendTtlMinMaxError(ttl);
        if (minMax) {
            summary.innerHTML = '<span class="files-status-warn">' + esc(minMax) + '</span>';
            return;
        }
        var expiry = Math.floor(Date.now() / 1000) + ttl;
        summary.innerHTML = '<span>' + esc(tparams('files.expiry_summary', { date: fmtDate(expiry) }, 'Expires ' + fmtDate(expiry))) + '</span>';
    }

    function sendTtlSeconds() {
        // Submit-path helper: submitSend validates the TTL before this is consulted
        var ttl = sendTtlSecondsStrict();
        return ttl !== null ? ttl : TTL_DEFAULT;
    }

    function sendTtlSecondsStrict() {
        var sel = getEl('filesSendExpiry');
        var mode = sel ? sel.value : String(TTL_DEFAULT);
        if (mode === 'custom') return customTtlSeconds().seconds;
        var n = parseInt(mode, 10);
        return Number.isFinite(n) && n > 0 ? n : TTL_DEFAULT;
    }

    function sendTtlMinMaxError(ttl) {
        var limits = sendTtlLimits();
        if (ttl < limits.min || ttl > limits.max) {
            return tparams('files.ttl_out_of_range', { min: durationText(limits.min), max: durationText(limits.max) },
                'Choose between ' + durationText(limits.min) + ' and ' + durationText(limits.max));
        }
        return null;
    }

    function sendGraceSeconds(ttl) {
        return ttl >= 604800 ? 86400 : 3600;
    }

    // the comment counter: only near the limit (the server counts bytes: 1000)
    var COMMENT_MAX_BYTES = 1000;
    function utf8Length(text) {
        try { return new TextEncoder().encode(text).length; } catch (e) { return String(text).length; }
    }
    function renderSendCommentLeft() {
        var input = getEl('filesSendComment');
        var out = getEl('filesSendCommentLeft');
        if (!input || !out) return;
        var left = COMMENT_MAX_BYTES - utf8Length(input.value || '');
        out.textContent = left <= 100
            ? plural('files.comment_left', Math.max(left, 0), '{count} character left', '{count} characters left')
            : '';
    }

    function sendSignature() {
        var recipient = getEl('filesSendRecipient');
        var provider = getEl('filesSendProvider');
        var file = getEl('filesSendFile');
        var expiry = getEl('filesSendExpiry');
        var comment = getEl('filesSendComment');
        var f = file && file.files && file.files.length ? file.files[0] : null;
        var ttl = sendTtlSecondsStrict(); // null only for an invalid Custom value
        return [
            recipient ? recipient.value : '',
            provider ? provider.value : '',
            expiry ? expiry.value : String(TTL_DEFAULT),
            ttl === null ? '' : String(ttl), // exact TTL in the semantic form
            ttl === null ? '' : String(sendGraceSeconds(ttl)), // exact grace too (Section 7)
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

    function isSendDialogOpen() {
        return Boolean(state.dialog && state.dialog.className &&
            String(state.dialog.className).indexOf('is-send') !== -1);
    }

    // G2: a generation is "current" only while it is the latest generation AND
    // its Send dialog is still open. Every async send-dialog response is gated
    // on this, so a slower response started for an older dialog can never update
    // a newer one (G2.2), and a response landing after close is dropped too.
    function sendGenerationCurrent(gen) {
        return gen === state.sendGeneration && isSendDialogOpen();
    }

    // G2: end the current Send-dialog generation - bump the generation token,
    // drop the authoritative readiness result, and cancel any pending debounce
    // so a stale readiness check cannot fire for a closed/replaced dialog.
    function invalidateSendGeneration() {
        state.sendGeneration++;
        state.sendReadinessToken++;
        state.sendReadiness = null;
        if (state.sendTtlDebounce) {
            clearTimeout(state.sendTtlDebounce);
            state.sendTtlDebounce = null;
        }
    }

    // F2: authoritative readiness for the exact requested TTL, rendered into a
    // dedicated element (filesSendReadinessCheck) separate from the config-only
    // Relay-state/upload line (filesSendReadiness). A monotonic token drops any
    // response that no longer matches the latest request, so a slower older
    // response or one landing after the dialog closed/advanced cannot overwrite
    // the current value.
    function renderSendReadinessCheck() {
        var el = getEl('filesSendReadinessCheck');
        if (!el) return;
        var s = state.sendReadiness;
        if (!s) { el.innerHTML = ''; return; }
        if (s.pending) {
            el.innerHTML = '<span>' + esc(t('files.readiness_checking', 'Checking upload…')) + '</span>';
            return;
        }
        if (s.reason === 'ttl_invalid') { el.innerHTML = ''; return; }       // the expiry field owns this message
        var label = s.ready
            ? t('files.upload_ready', 'Upload: ready')
            : t('files.upload_not_ready', 'Upload: not ready') + ' - ' + filesReadinessLabel(s.reason);
        el.innerHTML = '<span class="' + (s.ready ? 'files-status-ok' : 'files-status-warn') + '">' + esc(label) + '</span>';
    }

    // F2: gate the Send button on every prerequisite the server will re-check at
    // submit. This is proactive feedback only - never authoritative - and is
    // intentionally kept out of unlockSend(), which the tests rely on to set
    // disabled=false directly after a terminal command.
    function renderSendSubmit() {
        var submit = getEl('filesSendSubmit');
        if (!submit) return;
        if (state.sendInFlight) { submit.disabled = true; return; }
        var recipient = getEl('filesSendRecipient');
        var provider = getEl('filesSendProvider');
        var fileInput = getEl('filesSendFile');
        var hasRecipient = Boolean(recipient && recipient.value);
        var hasProvider = Boolean(provider && provider.value);
        var file = fileInput && fileInput.files && fileInput.files.length ? fileInput.files[0] : null;
        var fileOk = Boolean(file && fileTypeAllowed(file) &&
            (typeof file.size !== 'number' || file.size <= MAX_SEND_BYTES));
        if (state.sendPictures) {
            // pictures: 1 to 10 of them, and a recipient that is really sendable now (a key still missing is not)
            var picked = sendSelectedContact();
            fileOk = state.sendPictures.items.length >= 1 && state.sendPictures.items.length <= MAX_SEND_PICTURES;
            hasRecipient = Boolean(picked && picked.can_send_file === true);
        }
        var ttlValid = sendTtlSecondsStrict() !== null;
        var readinessOk = Boolean(state.sendReadiness && state.sendReadiness.ready);
        submit.disabled = !(hasRecipient && hasProvider && fileOk && ttlValid && readinessOk);
    }

    function refreshSendReadiness() {
        if (!isSendDialogOpen()) { state.sendReadiness = null; return; }
        var gen = state.sendGeneration; // G2: gate the async response on this dialog
        var sel = getEl('filesSendProvider');
        var providerId = sel ? sel.value : '';
        var ttl = sendTtlSecondsStrict();
        var token = ++state.sendReadinessToken;

        if (!providerId) {
            state.sendReadiness = null;
            renderSendReadinessCheck();
            renderSendSubmit();
            return;
        }
        if (ttl === null) {
            state.sendReadiness = { providerId: providerId, ttl: null, ready: false, reason: 'ttl_invalid', pending: false };
            renderSendReadinessCheck();
            renderSendSubmit();
            return;
        }

        state.sendReadiness = { providerId: providerId, ttl: ttl, ready: false, reason: 'checking', pending: true };
        renderSendReadinessCheck();
        renderSendSubmit();

        api('/api/mca/providers/' + encodeURIComponent(providerId) +
            '/upload-readiness?requested_ttl_seconds=' + ttl).then(function (r) {
            if (token !== state.sendReadinessToken) return; // stale response - drop
            if (!sendGenerationCurrent(gen)) return;          // dialog closed/advanced - drop
            var ready = Boolean(r.status === 200 && r.data && r.data.ok === true && r.data.ready);
            var reason = ready ? null : (r.data && r.data.reason ? r.data.reason : 'relay_unreachable');
            state.sendReadiness = { providerId: providerId, ttl: ttl, ready: ready, reason: reason, pending: false };
            renderSendReadinessCheck();
            renderSendSubmit();
        });
    }

    function scheduleSendReadinessRefresh() {
        if (state.sendTtlDebounce) clearTimeout(state.sendTtlDebounce);
        var gen = state.sendGeneration; // G2: the debounce is scoped to this dialog
        state.sendTtlDebounce = setTimeout(function () {
            state.sendTtlDebounce = null;
            if (!sendGenerationCurrent(gen)) return; // dialog closed/replaced - drop
            refreshSendReadiness();
        }, 300);
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
        if (state.sendPictures) { submitSendPictures(recipient, provider); return; }
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

        // a custom expiry must be a whole number in range (the message is under the field, here only the toast)
        var ttlCheck = sendTtlSecondsStrict();
        if (ttlCheck === null) {
            toast(t('files.err_ttl_custom', 'Enter a whole number greater than zero'), 'error');
            return;
        }
        var rangeError = sendTtlMinMaxError(ttlCheck);
        if (rangeError) { toast(rangeError, 'error'); return; }

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
                    noteRecipientUsed(recipient.value);
                    trackCommand(r2.data.command_id, {
                        resourceKey: 'create',
                        queued: t('files.sending', 'Sending file…'),
                        success: t('files.send_queued', 'Transfer created'),
                        onSuccess: function () {
                            unlockSend();
                            state.sendSignature = null;
                            state.sendClientRequestId = null;
                            // F5: close only after the authoritative refresh
                            // settles, and return its Promise so the tracker
                            // does not announce success early.
                            return refreshTransfers().then(function () {
                                closeModal(false);
                                if (attachmentId) { state.focusId = attachmentId; renderTransfers(); }
                            });
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
                            return refreshTransfers();
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
        if (!file) return false;
        var name = (file.name || '').toLowerCase();
        var ext = name.indexOf('.') !== -1 ? name.split('.').pop() : '';
        // F1: a recognized allowlisted extension OR a browser-supplied allowlisted
        // MIME (even extensionless) is allowed. When both are absent/unrecognized
        // the file is rejected locally - the server's magic-byte sniffing remains
        // authoritative, so this is a client-side gate, never a guarantee.
        return Boolean(EXTENSION_MIME[ext] || ALLOWED_MIME[file.type]);
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
                            '<div class="files-provider-probe-row">' +
                                '<input type="url" id="filesProviderOrigin" placeholder="https://relay.example.com" autocomplete="off" />' +
                                '<button type="button" class="files-action-btn" data-files-action="provider-probe">' + esc(t('files.probe', 'Probe')) + '</button>' +
                            '</div>' +
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
                    onSuccess: function () { return refreshAfterCommand('providers', loadProviders); },
                    onUnknown: function () { return refreshAfterCommand('providers', loadProviders); },
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

    // The user's own TTL limit (null = none). An older server that does not send the split falls back to the single value.
    function ownTtl(p, which) {
        var own = p['user_' + which + '_ttl_seconds'];
        var value = own !== undefined ? own : p[which + '_ttl_seconds'];
        return (value === null || value === undefined) ? null : value;
    }

    function providerEditFields(id) {
        var p = providerById(id);
        if (!p) return '';
        // FX1 Phase B: the fields hold the user's OWN limits; the relay's published limit is the placeholder (the stricter applies)
        var ownMin = ownTtl(p, 'min');
        var ownMax = ownTtl(p, 'max');
        var minTtl = ownMin === null ? '' : String(ownMin);
        var maxTtl = ownMax === null ? '' : String(ownMax);
        var ttlPlaceholder = t('files.provider_ttl_unset', 'Unset');
        var relayMin = (p.relay_min_ttl_seconds === null || p.relay_min_ttl_seconds === undefined) ? '' : String(p.relay_min_ttl_seconds);
        var relayMax = (p.relay_max_ttl_seconds === null || p.relay_max_ttl_seconds === undefined) ? '' : String(p.relay_max_ttl_seconds);
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
                '<input type="number" id="filesEditMinTtl-' + esc(id) + '" min="1" step="1" value="' + esc(minTtl) + '" placeholder="' + esc(relayMin ? relayMin : ttlPlaceholder) + '" autocomplete="off" />' +
            '</div>' +
            '<div class="files-edit-field">' +
                '<label for="filesEditMaxTtl-' + esc(id) + '">' + esc(t('files.provider_ttl_max', 'Max TTL (seconds)')) + '</label>' +
                '<input type="number" id="filesEditMaxTtl-' + esc(id) + '" min="1" step="1" value="' + esc(maxTtl) + '" placeholder="' + esc(relayMax ? relayMax : ttlPlaceholder) + '" autocomplete="off" />' +
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

        var oldMin = ownTtl(p, 'min');
        var oldMax = ownTtl(p, 'max');
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
            // Nothing changed - no pointless PATCH; just dismiss.
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
            list.innerHTML = '<div class="files-empty">' + esc(t('files.no_providers', 'No Relay providers configured.')) +
                '<br/><button type="button" class="files-action-btn" data-files-action="relay-wizard">' +
                    esc(t('files.wizard_setup_relay', 'Set up a Relay')) + '</button></div>';
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
                        providerField('files.provider_tls', 'TLS required', p.tls_required ? '✓' : '-') +
                        providerField('files.provider_upload_allowed', 'Upload allowed', p.upload_allowed ? '✓' : '-') +
                        providerField('files.provider_download_allowed', 'Download allowed', p.download_allowed ? '✓' : '-') +
                        providerField('files.provider_ttl_min', 'Min TTL', p.min_ttl_seconds ? fmtGrace(p.min_ttl_seconds) : '-') +
                        providerField('files.provider_ttl_max', 'Max TTL', p.max_ttl_seconds ? fmtGrace(p.max_ttl_seconds) : '-') +
                        providerField('files.provider_max_bytes', 'Max ciphertext bytes', fmtBytes(p.max_ciphertext_bytes)) +
                        providerField('files.provider_protocol', 'Protocol version', p.protocol_version || '-') +
                        providerField('files.provider_token', 'Token', p.upload_token_configured ? '✓' : '-') +
                    '</div>' +
                    '<div class="files-provider-fields files-provider-status">' +
                        providerField('files.provider_upload_readiness', 'Upload readiness', providerUploadReadiness(p)) +
                        providerField('files.provider_last_checked', 'Last checked', providerLastChecked(p)) +
                        providerField('files.provider_last_check_result', 'Last check result', providerLastCheckResult(p)) +
                        providerField('files.provider_last_latency', 'Latency', providerLastLatency(p)) +
                        providerField('files.provider_last_error', 'Last error', providerLastError(p)) +
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

    // F3: the shared provider card's live status fields. Values are localized
    // (local-timezone timestamp, numeric-only latency, safe code/fallback) and
    // never expose secrets or raw exception text.
    function providerLastChecked(p) {
        return p.last_checked_at ? fmtDate(p.last_checked_at) : t('files.provider_never_checked', 'Never checked');
    }
    function providerLastCheckResult(p) {
        return p.last_check_result ? filesRelayStateLabel(p.last_check_result) : '-';
    }
    function providerLastLatency(p) {
        return (typeof p.last_latency_ms === 'number' && Number.isFinite(p.last_latency_ms))
            ? p.last_latency_ms + ' ms' : '-';
    }
    function providerLastError(p) {
        var code = p && p.last_error_code ? String(p.last_error_code) : '';
        if (!code) return '-';
        // http_<3 digits> -> localized template carrying only the numeric status
        // (never the raw code string), so a provider returning 503 reads "HTTP 503".
        var http = /^http_(\d{3})$/.exec(code);
        if (http) {
            return tparams('files.provider_error.http_status', { status: http[1] }, 'HTTP ' + http[1]);
        }
        // unsupported_protocol_version:<value> -> localized; the value is a
        // protocol version, not user/secret text, but it is still not echoed
        // verbatim - the label carries the meaning without the raw suffix.
        if (code.indexOf('unsupported_protocol_version:') === 0) {
            return t('files.provider_error.unsupported_protocol_version', 'Unsupported protocol version');
        }
        var mapped = PROVIDER_ERROR_CODE_LABELS[code];
        if (mapped) return t(mapped.key, mapped.fallback);
        // Any other (unknown) raw code is never rendered verbatim.
        return t('files.provider_error.unknown', 'Check failed');
    }
    function providerUploadReadiness(p) {
        return filesUploadReadinessLabel(p.upload_readiness);
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
                        return refreshAfterCommand('providers', loadProviders);
                    },
                    onUnknown: function () { return refreshAfterCommand('providers', loadProviders); },
                });
            }).catch(function () {
                toast(t('files.error.network_error', 'Network error'), 'error');
            });
        });
    }

    // ---- Relay setup wizard (URL -> fingerprint -> token -> readiness -> done) --
    //
    // Step-by-step onboarding for the Relay probe/register/token/readiness flow
    // that `openProviderSettings()` already exposes as one flat dialog. This
    // wizard is a pure frontend re-sequencing of the SAME existing endpoints
    // (POST /api/mca/providers/probe, POST /api/mca/providers, PUT
    // /api/mca/providers/{id}/upload-token, GET .../upload-readiness) - no new
    // backend contract. Adding a second/third Relay stays reachable only from
    // the existing "Relay providers" advanced dialog (the wizard's "done" step
    // links there), never from this wizard itself.

    var DEFAULT_RELAY_URL = 'https://mcattach.elektroniker.help';

    function relayWizardBody() { return getEl('filesWizardBody'); }
    function relayWizardFooter() { return getEl('filesWizardFooter'); }
    function relayWizardError(msg) {
        var el = getEl('filesWizardError');
        if (el) el.textContent = msg || '';
    }

    function openRelayWizard() {
        if (typeof document === 'undefined') return;
        closeModal();
        state.relayWizard = { step: 'url', origin: DEFAULT_RELAY_URL, probe: null, providerId: null, ready: false, reason: null };
        var id = 'files-relay-wizard';
        var html =
            '<div class="files-modal-backdrop" data-files-action="modal-backdrop-close" role="presentation">' +
                '<div class="files-modal" role="dialog" aria-modal="true" aria-labelledby="' + id + '-title">' +
                    '<div class="files-modal-header">' +
                        '<h3 class="files-modal-title" id="' + id + '-title">🧭 ' + esc(t('files.wizard_title', 'Connect a Relay')) + '</h3>' +
                        '<button type="button" class="files-modal-close" data-files-action="modal-close" aria-label="' + esc(t('files.dialog_close', 'Close dialog')) + '">×</button>' +
                    '</div>' +
                    '<div class="files-modal-body" id="filesWizardBody"></div>' +
                    '<div class="files-modal-footer" id="filesWizardFooter"></div>' +
                '</div>' +
            '</div>';
        openModalHtml(html, function () { state.relayWizard = null; });
        renderRelayWizardStep('url');
    }

    function renderRelayWizardStep(step) {
        var w = state.relayWizard;
        if (!w) return;
        w.step = step;
        var body = relayWizardBody();
        var footer = relayWizardFooter();
        if (!body || !footer) return;

        if (step === 'url') {
            body.innerHTML =
                '<p class="files-wizard-hint">' + esc(t('files.wizard_intro', 'Add a Relay so this device can send and receive files with MCAttach.')) + '</p>' +
                '<div class="files-field">' +
                    '<label for="filesWizardUrl">' + esc(t('files.wizard_url_label', 'Relay URL')) + '</label>' +
                    '<input type="url" id="filesWizardUrl" value="' + esc(w.origin || DEFAULT_RELAY_URL) + '" autocomplete="off" />' +
                '</div>' +
                '<p class="files-wizard-hint">' + esc(t('files.wizard_url_hint', "Most people can leave this as-is - it's already set to the project's default Relay.")) + '</p>' +
                '<div id="filesWizardError" class="files-provider-edit-error" role="alert"></div>';
            footer.innerHTML =
                '<button type="button" class="files-modal-cancel" data-files-action="modal-cancel">' + esc(t('common.cancel', 'Cancel')) + '</button>' +
                '<button type="button" class="files-action-btn" data-files-action="wizard-probe">' + esc(t('files.wizard_next', 'Next')) + '</button>';
            return;
        }

        if (step === 'fingerprint') {
            var probe = w.probe || {};
            var fp = probe.service_key_fingerprint || '';
            body.innerHTML =
                '<h4 class="files-provider-add-title">' + esc(t('files.wizard_step_fingerprint_title', "Confirm the Relay's identity")) + '</h4>' +
                '<div class="files-provider-probe-line">' + esc(t('files.probe_origin', 'Origin')) + ': ' + esc(probe.origin || '') + '</div>' +
                '<div class="files-provider-probe-line">' + esc(t('files.probe_fingerprint', 'Service key fingerprint')) + ': <code>' + esc(groupFp(fp)) + '</code></div>' +
                '<p class="files-wizard-hint">' + esc(t('files.wizard_fingerprint_hint', "Compare this fingerprint with the one published by the Relay's operator. Only continue if they match exactly.")) + '</p>' +
                '<div class="files-field">' +
                    '<label for="filesWizardName">' + esc(t('files.provider_name', 'Provider name')) + '</label>' +
                    '<input type="text" id="filesWizardName" value="' + esc(w.displayName || probe.origin || '') + '" autocomplete="off" />' +
                '</div>' +
                '<label class="files-field files-field-inline"><select id="filesWizardKind">' +
                    '<option value="own"' + (w.kind !== 'third_party' ? ' selected' : '') + '>' + esc(t('files.provider_kind_own', 'Own')) + '</option>' +
                    '<option value="third_party"' + (w.kind === 'third_party' ? ' selected' : '') + '>' + esc(t('files.provider_kind_third_party', 'Third-party')) + '</option>' +
                '</select></label>' +
                '<div id="filesWizardError" class="files-provider-edit-error" role="alert"></div>';
            footer.innerHTML =
                '<button type="button" class="files-modal-cancel" data-files-action="wizard-back-url">' + esc(t('files.wizard_back', 'Back')) + '</button>' +
                '<button type="button" class="files-action-btn" data-files-action="wizard-confirm">' + esc(t('files.register', 'Confirm & register')) + '</button>';
            return;
        }

        if (step === 'token') {
            body.innerHTML =
                '<h4 class="files-provider-add-title">' + esc(t('files.wizard_step_token_title', 'Upload access token')) + '</h4>' +
                '<p class="files-wizard-hint">' + esc(t('files.wizard_token_hint', 'If you have an upload access token for this Relay, enter it now. You can add or change it later in advanced settings.')) + '</p>' +
                '<input type="password" id="filesWizardToken" autocomplete="off" placeholder="' + esc(t('files.wizard_token_placeholder', 'Upload access token (optional)')) + '" />' +
                '<div id="filesWizardError" class="files-provider-edit-error" role="alert"></div>';
            footer.innerHTML =
                '<button type="button" class="files-modal-cancel" data-files-action="wizard-skip-token">' + esc(t('files.wizard_skip_token', 'Skip for now')) + '</button>' +
                '<button type="button" class="files-action-btn" data-files-action="wizard-save-token">' + esc(t('files.wizard_save_continue', 'Save & continue')) + '</button>';
            return;
        }

        if (step === 'readiness') {
            body.innerHTML = '<p class="files-wizard-hint">' + esc(t('files.wizard_checking', 'Checking the Relay…')) + '</p>';
            footer.innerHTML = '';
            relayWizardCheckReadiness();
            return;
        }

        if (step === 'done') {
            var ready = !!w.ready;
            var title = ready ? t('files.wizard_ready_title', 'Relay ready') : t('files.wizard_not_ready_title', 'Not ready yet');
            var bodyMsg = ready ? t('files.wizard_ready_body', 'This Relay is registered and ready to send files.')
                : t('files.wizard_not_ready_body', "The Relay is registered, but sending isn't ready yet:");
            body.innerHTML =
                '<h4 class="files-provider-add-title">' + esc(title) + '</h4>' +
                '<p>' + esc(bodyMsg) + '</p>' +
                (ready ? '' : '<p class="files-wizard-hint">' + esc(filesReadinessLabel(w.reason)) + '</p>');
            footer.innerHTML =
                (ready ? '' : '<button type="button" class="files-modal-cancel" data-files-action="wizard-check-readiness">' + esc(t('files.wizard_retry_check', 'Check again')) + '</button>') +
                '<button type="button" class="files-action-btn" data-files-action="wizard-advanced">' + esc(t('files.wizard_manage_advanced', 'Manage Relays (advanced)')) + '</button>' +
                '<button type="button" class="files-modal-submit" data-files-action="wizard-finish">' + esc(t('files.wizard_finish', 'Finish')) + '</button>';
            return;
        }
    }

    function relayWizardProbe() {
        var w = state.relayWizard;
        if (!w) return;
        var input = getEl('filesWizardUrl');
        var origin = input ? input.value.trim() : '';
        if (!origin) {
            relayWizardError(t('files.err_no_origin', 'Enter a provider URL'));
            return;
        }
        w.origin = origin;
        relayWizardError('');
        api('/api/mca/providers/probe', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ base_url: origin }),
        }).then(function (r) {
            if (r.status !== 202 || !r.data || !r.data.command_id) {
                relayWizardError(filesErrorCode(r.data));
                return;
            }
            trackCommand(r.data.command_id, {
                resourceKey: 'relay-wizard',
                queued: t('files.probing', 'Probing provider…'),
                success: t('files.probe_ok', 'Provider reached'),
                onSuccess: function (cmd) {
                    if (!state.relayWizard) return;
                    state.relayWizard.probe = (cmd && cmd.result) || {};
                    renderRelayWizardStep('fingerprint');
                },
                onFailed: function (cmd) { relayWizardError(filesErrorCode(cmd)); },
            });
        }).catch(function () {
            relayWizardError(t('files.error.network_error', 'Network error'));
        });
    }

    function relayWizardConfirm() {
        var w = state.relayWizard;
        if (!w || !w.probe || !w.probe.probe_id) return;
        var nameInput = getEl('filesWizardName');
        var kindInput = getEl('filesWizardKind');
        w.displayName = (nameInput && nameInput.value.trim()) ? nameInput.value.trim() : (w.probe.origin || '');
        w.kind = (kindInput && kindInput.value) ? kindInput.value : 'own';
        var fingerprint = w.probe.service_key_fingerprint || '';
        relayWizardError('');

        api('/api/mca/providers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                probe_id: w.probe.probe_id,
                fingerprint_confirmation: fingerprint,
                display_name: w.displayName,
                policy: { kind: w.kind, upload_allowed: true, download_allowed: true },
            }),
        }).then(function (r) {
            if (r.status !== 202 || !r.data || !r.data.command_id) {
                relayWizardError(filesErrorCode(r.data));
                return;
            }
            trackCommand(r.data.command_id, {
                resourceKey: 'relay-wizard',
                queued: t('files.registering', 'Registering provider…'),
                success: t('files.provider_registered', 'Provider registered'),
                onSuccess: function (cmd) {
                    if (!state.relayWizard) return;
                    state.relayWizard.providerId = (cmd && cmd.result) ? cmd.result.provider_id : null;
                    return refreshAfterCommand('providers', loadProviders).then(function () {
                        if (state.relayWizard) renderRelayWizardStep('token');
                    });
                },
                onFailed: function (cmd) { relayWizardError(filesErrorCode(cmd)); },
            });
        }).catch(function () {
            relayWizardError(t('files.error.network_error', 'Network error'));
        });
    }

    function relayWizardSaveToken() {
        var w = state.relayWizard;
        if (!w || !w.providerId) return;
        var input = getEl('filesWizardToken');
        var token = input ? input.value.trim() : '';
        if (!token) {
            renderRelayWizardStep('readiness');
            return;
        }
        relayWizardError('');
        api('/api/mca/providers/' + encodeURIComponent(w.providerId) + '/upload-token', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ upload_token: token }),
        }).then(function (r) {
            if (r.status !== 202 || !r.data || !r.data.command_id) {
                relayWizardError(filesErrorCode(r.data));
                return;
            }
            trackCommand(r.data.command_id, {
                resourceKey: 'relay-wizard',
                queued: t('files.saving_token', 'Saving token…'),
                success: t('files.token_saved', 'Upload token saved'),
                onSuccess: function () {
                    return refreshAfterCommand('providers', loadProviders).then(function () {
                        if (state.relayWizard) renderRelayWizardStep('readiness');
                    });
                },
                onFailed: function (cmd) { relayWizardError(filesErrorCode(cmd)); },
            });
        }).catch(function () {
            relayWizardError(t('files.error.network_error', 'Network error'));
        });
    }

    function relayWizardCheckReadiness() {
        var w = state.relayWizard;
        if (!w || !w.providerId) return;
        api('/api/mca/providers/' + encodeURIComponent(w.providerId) + '/upload-readiness').then(function (r) {
            if (!state.relayWizard) return;
            if (r.status === 200 && r.data && r.data.ok) {
                state.relayWizard.ready = !!r.data.ready;
                state.relayWizard.reason = r.data.reason;
            } else {
                state.relayWizard.ready = false;
                state.relayWizard.reason = null;
            }
            renderRelayWizardStep('done');
        }).catch(function () {
            if (!state.relayWizard) return;
            state.relayWizard.ready = false;
            state.relayWizard.reason = null;
            renderRelayWizardStep('done');
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
            if (typeof window !== 'undefined' && window.addEventListener) {
                // the vault was unlocked or locked: the list shows names again / a placeholder
                window.addEventListener('mcvault:change', function () { if (state.active) refresh(); });
            }
            // 'toggle' does not bubble, so only the capture phase reaches a delegated listener for the dynamic <details>
            document.addEventListener('toggle', onMoreToggle, true);
            // a thumbnail that cannot be made (not an image after all, a locked vault, a gone file) falls back to the type icon
            document.addEventListener('error', onThumbError, true);
            // long press on a card (touch) starts selecting
            document.addEventListener('pointerdown', onCardPointerDown);
            document.addEventListener('pointerup', cancelLongPress);
            document.addEventListener('pointermove', cancelLongPress);
            document.addEventListener('pointercancel', cancelLongPress);
        }
        // Direct input listener for the search box (delegation covers dynamic content,
        // but the search box is static markup so bind it directly too for clarity).
        var search = getEl('filesSearch');
        if (search && search.addEventListener && !search._filesBound) {
            search._filesBound = true;
            search.addEventListener('input', onDocumentInput);
        }
    }

    // ---- "More details": the technical rows are in the card; the timeline is fetched when it is first opened ---------------
    function moreExtraHtml(a, timeline) {
        var deliveries = (Array.isArray(a.deliveries) && a.deliveries.length) ? a.deliveries : [];
        var parts = [];
        if (deliveries.length) {
            parts.push('<div class="files-detail-subtitle">' + esc(t('files.detail_deliveries', 'Deliveries')) + '</div>' + deliveries.map(function (d) {
                return '<div class="files-timeline-event"><span class="files-timeline-time">' + esc(fmtDate(d.sent_at)) + '</span><span>' +
                    esc((d.route_type || '') + ' ' + (d.state || '')) + '</span></div>';
            }).join(''));
        }
        if (timeline.length) {
            parts.push(timeline.map(function (e) {
                return '<div class="files-timeline-event"><span class="files-timeline-time">' + esc(fmtDate(e.created_at || e.at)) + '</span>' +
                    '<span>' + esc(e.event_type || '') + '</span></div>';
            }).join(''));
        }
        return parts.join('');
    }

    function onMoreToggle(e) {
        var el = e && e.target;
        if (!el || !el.getAttribute || !el.hasAttribute || !el.hasAttribute('data-more')) return;
        var id = el.getAttribute('data-more');
        if (el.open) state.openMore[id] = true; else delete state.openMore[id];
        if (!el.open || state.moreExtra[id] !== undefined) return;
        state.moreExtra[id] = '';
        api('/api/attachments/' + encodeURIComponent(id)).then(function (r) {
            if (r.status !== 200 || !r.data || r.data.ok !== true) { delete state.moreExtra[id]; return; }
            var html = moreExtraHtml(r.data.attachment || {}, Array.isArray(r.data.timeline) ? r.data.timeline : []);
            state.moreExtra[id] = html;
            var target = document.querySelector ? document.querySelector('[data-more-extra="' + id + '"]') : null;
            if (target) target.innerHTML = html;
        }).catch(function () { delete state.moreExtra[id]; });
    }

    function onThumbError(e) {
        var el = e && e.target;
        if (!el || el.tagName !== 'IMG' || !el.hasAttribute || !el.hasAttribute('data-thumb')) return;
        var holder = el.parentNode;
        var card = closestSel(holder, '.files-card');
        var a = card ? state.attachments.filter(function (x) { return x.id === card.getAttribute('data-attachment'); })[0] : null;
        if (holder) holder.textContent = mimeIcon(a ? a.mime_type : '');
    }

    var longPressTimer = null;
    function cancelLongPress() { if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; } }
    function onCardPointerDown(e) {
        if (!e || e.pointerType !== 'touch' || state.selectMode) return;
        var card = closestSel(e.target, '.files-card');
        if (!card || closestSel(e.target, 'button, a, summary, input')) return;
        var id = card.getAttribute('data-attachment');
        cancelLongPress();
        longPressTimer = setTimeout(function () {
            longPressTimer = null;
            state.selectMode = true;
            state.checked[id] = true;
            state.longPressed = true;
            renderTransfers();
        }, 500);
    }

    function currentFilesSettings() {
        var s = (typeof appSettings !== 'undefined' && appSettings && appSettings.files) ? appSettings.files : {};
        return { auto_accept: s.auto_accept !== false, auto_cleanup_days: Number(s.auto_cleanup_days) || 0 };
    }

    function cleanupOptionLabel(days) {
        return Number(days) === 0 ? t('settings.files_cleanup_never', 'Never') : plural('files.cleanup_days', Number(days), '{count} day', '{count} days');
    }

    function syncFilesSettings() {
        var current = currentFilesSettings();
        var accept = getEl('filesAutoAccept');
        if (accept) accept.checked = current.auto_accept;
        var cleanup = getEl('filesAutoCleanup');
        if (cleanup) {
            var options = cleanup.querySelectorAll ? cleanup.querySelectorAll('option') : [];
            for (var i = 0; i < options.length; i++) options[i].textContent = cleanupOptionLabel(options[i].value);
            cleanup.value = String(current.auto_cleanup_days);
        }
    }

    function saveFilesSetting(name, value) {
        var body = { files: {} };
        body.files[name] = value;
        return api('/api/settings', { method: 'POST', body: JSON.stringify(body) }).then(function (r) {
            if (r.status === 200 && r.data && r.data.ok) {
                if (typeof appSettings !== 'undefined') appSettings = r.data.settings;
            } else {
                toast(t('files.settings_save_failed', 'Could not save the setting'), 'error');
            }
            syncFilesSettings();
        }).catch(function () { toast(t('files.settings_save_failed', 'Could not save the setting'), 'error'); syncFilesSettings(); });
    }

    function onFilesSettingChange(target) {
        var name = target.getAttribute('data-files-setting');
        if (name === 'auto_accept') { saveFilesSetting(name, Boolean(target.checked)); return; }
        if (name !== 'auto_cleanup_days') return;
        var days = parseInt(target.value, 10) || 0;
        if (days === 0) { saveFilesSetting(name, 0); return; }
        // turning on an automatic deletion is a decision: say what will happen, once
        var dialog = (typeof window !== 'undefined') ? window.MCDialog : null;
        var asked = (dialog && dialog.confirm) ? dialog.confirm({
            title: t('files.cleanup_confirm_title', 'Delete old received files automatically?'),
            message: plural('files.cleanup_confirm_body', days,
                'Received files older than {count} day will be deleted from this node, about once an hour. The entries stay in the history.',
                'Received files older than {count} days will be deleted from this node, about once an hour. The entries stay in the history.'),
            confirmLabel: t('files.cleanup_confirm', 'Turn on'), cancelLabel: t('dialog.cancel', 'Cancel'), danger: true,
        }) : Promise.resolve(false);
        asked.then(function (yes) { if (yes) saveFilesSetting(name, days); else syncFilesSettings(); });
    }

    // the node card's quick links (chat.js) ask for "received" / "sent" / "all"
    function setFilterCompat(direction) {
        setSegment(direction === 'received' ? 'incoming' : (direction === 'sent' ? 'sent' : 'all'));
    }

    bindListeners();

    window.MeshCenterFiles = {
        plainStatus: plainStatus,
        isExecutableName: isExecutableName,
        setSegment: setSegment,
        syncSettings: syncFilesSettings,
        activate: activate,
        deactivate: deactivate,
        refresh: refresh,
        // PR 5 final correction (section 2): expose the contact busy state so the
        // sidebar node-card key row can disable its MCA action buttons while a
        // confirm/accept/reject/request-key command is in flight for that node.
        isContactBusy: function (contactId) {
            return Boolean(state.busy['contact:' + contactId]);
        },
        // U3: the map's node menu / popup "Request key" - the same function
        // the node card's Request key button runs (data-files-action).
        requestKey: function (contactId) { contactRequestKey(contactId); },
        // gallery G2: the Send dialog with pictures from the Media gallery (one transfer per picture)
        openSendPictures: openSendPictures,
        maxSendPictures: MAX_SEND_PICTURES,
        // MCA-UX1: a chat bubble opens its transfer; the tab dot keeps counting while Files is not shown
        selectTransfer: selectTransferFromOutside,
        startBadge: startBadge,
    };

    // Static compatibility entry points referenced by the existing template
    // header buttons / filter markup (literal values only, no dynamic ids).
    window.openFilesWorkspace = activate;
    window.closeFilesWorkspace = deactivate;
    window.loadFilesWorkspace = refresh;
    window.setFilesFilter = setFilterCompat;
    window.openFilesSendDialog = openSendDialog;
    window.closeFilesSendDialog = function () { closeModal(false); };
    window.openFilesProviderSettings = openProviderSettings;
    window.closeFilesProviderSettings = function () { closeModal(false); };
    // Step-by-step onboarding wizard (Settings "Set up a Relay" button) - a
    // distinct entry point from the flat "Relay providers" advanced dialog
    // above; both operate on the same state and the same backend endpoints.
    window.openFilesRelaySetupWizard = function () {
        if (typeof window.switchMainTab === 'function') window.switchMainTab('files');
        openRelayWizard();
    };
    // Entry point used by the Settings workspace "Relay providers" control: it
    // navigates to the Files workspace (which activates the module) and reuses
    // the SAME provider component - no second implementation or cache.
    window.openFilesRelayProviders = function () {
        if (typeof window.switchMainTab === 'function') window.switchMainTab('files');
        openProviderSettings();
    };
})();
