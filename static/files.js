/* static/files.js
 *
 * The Files workspace — MeshCenter's end-to-end encrypted file-transfer UI
 * (MCAttach). Loaded before chat.js so chat.js's `switchMainTab('files')`
 * can call `openFilesWorkspace()` with a `typeof` guard (the same pattern as
 * media.js / chat-camera.js). No build step: a plain script whose top-level
 * functions become globals, matching the other domain-split files.
 *
 * Backend contract (api/api_attachments.py):
 *   - GET  /api/mca/contacts                          -> contacts + trust state
 *   - GET  /api/attachments                           -> transfer archive
 *   - GET  /api/attachments/{id}                      -> one transfer + timeline
 *   - GET  /api/attachments/{id}/content              -> verified plaintext
 *   - POST /api/attachments                           -> multipart create (202)
 *   - POST /api/attachments/{id}/download|reject|save|cancel|revoke|retry
 *   - DELETE /api/attachments/{id}/local-content
 *   - POST /api/mca/contacts/{id}/request-key|confirm|key-change/accept|key-change/reject
 *   - GET  /api/mca/providers, POST /api/mca/providers/probe, POST /api/mca/providers
 *   - PATCH/DELETE /api/mca/providers/{id}, POST /api/mca/providers/{id}/default
 *   - PUT/DELETE /api/mca/providers/{id}/upload-token, POST /api/mca/providers/{id}/check
 *
 * Every mutating request is a plain `fetch` — static/csrf.js monkey-patches
 * window.fetch to add the CSRF header to same-origin mutating /api/ requests.
 * All interpolated values are escaped through `escapeHtml()` (a chat.js
 * global), and user-facing strings go through `I18N.t()` / `I18N.tOrFallback()`.
 */

'use strict';

// ---- module state ---------------------------------------------------------

const FILES_POLL_MS = 5000;
const FILES_MAX_SEND_BYTES = 5 * 1024 * 1024; // mirrors _MAX_FILE_BYTES server-side

let filesPollTimer = null;
let filesFilter = 'all';
let filesContacts = [];
let filesTransfers = [];
let filesSelectedId = null;
let filesDialogEl = null;   // the currently-open modal (send or provider settings)
let filesLastProbe = null;  // the last successful provider_probe result (pending registration)

// Human-readable fallback labels. i18n keys (files.state.*, files.contact.*)
// are the source of truth once the catalogs carry them; these are the
// graceful fallback so the workspace is never blank before translation lands.
const FILES_STATE_LABELS = {
    DRAFT: 'Draft', VALIDATING: 'Validating', ENCRYPTING: 'Encrypting',
    QUEUED_UPLOAD: 'Queued for upload', UPLOADING: 'Uploading', READY_TO_SEND: 'Ready to send',
    SENT: 'Sent', RECEIVED: 'Received', DOWNLOADED: 'Downloaded',
    EXPIRED: 'Expired', REVOKED: 'Revoked', CANCELLED: 'Cancelled',
    FAILED_VALIDATION: 'Validation failed', FAILED_UPLOAD: 'Upload failed', FAILED_RADIO: 'Radio send failed',
    OFFER_RECEIVED: 'Offer received', WAITING_KEY: 'Waiting for key', WAITING_PROVIDER: 'Waiting for provider',
    WAITING_NETWORK: 'Waiting for network', WAITING_CONSENT: 'Waiting for consent', DOWNLOADING: 'Downloading',
    VERIFYING: 'Verifying', AVAILABLE: 'Available', REJECTED: 'Rejected', FAILED: 'Failed',
};

const FILES_CONTACT_STATUS_LABELS = {
    trusted: 'Trusted', confirmation_required: 'Confirmation required',
    key_changed: 'Key changed', key_unknown: 'Key unknown',
};

// ---- small helpers ---------------------------------------------------------

function filesStateLabel(state) {
    return window.I18N.tOrFallback('files.state.' + state, {}, FILES_STATE_LABELS[state] || state);
}

function filesContactStatusLabel(status) {
    return window.I18N.tOrFallback('files.contact.' + status, {}, FILES_CONTACT_STATUS_LABELS[status] || status);
}

function filesErrorCode(data) {
    // The server error envelope is { ok:false, error, error_code }. Prefer the
    // translated code, fall back to the server's own English string.
    const code = data && data.error_code ? data.error_code : 'internal_error';
    const fallback = (data && data.error) || code;
    return window.I18N.tOrFallback('files.error.' + code, {}, fallback);
}

async function filesApi(url, options) {
    let resp;
    try {
        resp = await fetch(url, options);
    } catch (err) {
        return { status: 0, data: { ok: false, error: String(err && err.message || err), error_code: 'network_error' } };
    }
    let data = null;
    try {
        data = await resp.json();
    } catch (_) {
        data = { ok: false, error: 'HTTP ' + resp.status, error_code: 'http_' + resp.status };
    }
    return { status: resp.status, data };
}

function filesFormatBytes(n) {
    if (n === null || n === undefined) return '—';
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
}

function filesFormatDate(epochSeconds) {
    if (!epochSeconds) return '—';
    const d = new Date(epochSeconds * 1000);
    return d.toLocaleString();
}

async function filesPollCommand(commandId) {
    // Poll GET /api/mca/commands/{id} until the worker reaches a terminal
    // status (succeeded/failed), then return the serialized command. Bounded
    // (~40 x 800ms) so a stuck command never polls forever.
    for (let attempt = 0; attempt < 40; attempt++) {
        await new Promise(function (resolve) { setTimeout(resolve, 800); });
        const { status, data } = await filesApi('/api/mca/commands/' + encodeURIComponent(commandId));
        if (status !== 200 || !data || data.ok !== true || !data.command) return null;
        if (data.command.status === 'succeeded' || data.command.status === 'failed') return data.command;
    }
    return null;
}

// ---- workspace shell / navigation (#124) ----------------------------------

function openFilesWorkspace() {
    if (typeof filesPollTimer === 'number') startFilesPolling();
    loadFilesWorkspace(false);
}

function closeFilesWorkspace() {
    stopFilesPolling();
}

function loadFilesWorkspace(force) {
    loadFilesContacts();
    loadFilesTransfers(force);
    // Re-render the selected detail so it tracks live state while open.
    if (filesSelectedId) renderFilesDetail(filesSelectedId, false);
}

// ---- contacts (#124 + trust actions) --------------------------------------

async function loadFilesContacts() {
    const { status, data } = await filesApi('/api/mca/contacts');
    if (status !== 200 || !data || data.ok !== true) {
        if (status === 503) renderFilesContactsError(window.I18N.tOrFallback('files.not_ready', {}, 'MCAttach service is not ready'));
        else renderFilesContactsError(filesErrorCode(data));
        return;
    }
    filesContacts = Array.isArray(data.contacts) ? data.contacts : [];
    renderFilesContacts();
}

function renderFilesContactsError(message) {
    const list = document.getElementById('filesContactsList');
    if (!list) return;
    filesContacts = [];
    list.innerHTML = '<div class="files-empty">' + escapeHtml(message) + '</div>';
}

function renderFilesContacts() {
    const list = document.getElementById('filesContactsList');
    if (!list) return;
    if (!filesContacts.length) {
        list.innerHTML = '<div class="files-empty">' + escapeHtml(window.I18N.tOrFallback('files.no_contacts', {}, 'No known contacts yet.')) + '</div>';
        return;
    }
    list.innerHTML = filesContacts.map(function (c) {
        const status = filesContactStatusLabel(c.status);
        const shortFp = c.fingerprint ? c.fingerprint.slice(0, 16) : '';
        const trust = filesContactTrustActions(c);
        return (
            '<div class="files-contact-item' + (c.status === 'trusted' ? ' is-trusted' : '') + '">' +
                '<div class="files-contact-head">' +
                    '<span class="files-contact-id">' + escapeHtml(c.contact_id) + '</span>' +
                    '<span class="files-contact-status files-status-' + escapeHtml(c.status) + '">' + escapeHtml(status) + '</span>' +
                '</div>' +
                '<div class="files-contact-fp">' + escapeHtml(shortFp) + '</div>' +
                trust +
            '</div>'
        );
    }).join('');
}

function filesContactTrustActions(c) {
    if (c.status === 'confirmation_required') {
        return '<div class="files-contact-actions">' +
            '<button type="button" class="files-action-btn" onclick="filesContactConfirm(\'' + c.contact_id + '\')">' +
                escapeHtml(window.I18N.tOrFallback('files.trust_confirm', {}, 'Trust key')) +
            '</button></div>';
    }
    if (c.status === 'key_changed') {
        return '<div class="files-contact-actions">' +
            '<button type="button" class="files-action-btn" onclick="filesContactAcceptKeyChange(\'' + c.contact_id + '\')">' +
                escapeHtml(window.I18N.tOrFallback('files.key_change_accept', {}, 'Accept')) +
            '</button>' +
            '<button type="button" class="files-action-btn is-danger" onclick="filesContactRejectKeyChange(\'' + c.contact_id + '\')">' +
                escapeHtml(window.I18N.tOrFallback('files.key_change_reject', {}, 'Reject')) +
            '</button></div>';
    }
    if (c.status === 'key_unknown') {
        return '<div class="files-contact-actions">' +
            '<button type="button" class="files-action-btn" onclick="filesContactRequestKey(\'' + c.contact_id + '\')">' +
                escapeHtml(window.I18N.tOrFallback('files.request_key', {}, 'Request key')) +
            '</button></div>';
    }
    return '';
}

async function filesContactRequestKey(contactId) {
    const { status, data } = await filesApi('/api/mca/contacts/' + encodeURIComponent(contactId) + '/request-key', { method: 'POST' });
    if (status === 202) {
        showToast(window.I18N.tOrFallback('files.request_key_sent', {}, 'Key request queued'), 'info');
        setTimeout(loadFilesContacts, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}

async function filesContactConfirm(contactId) {
    const { status, data } = await filesApi('/api/mca/contacts/' + encodeURIComponent(contactId) + '/confirm', { method: 'POST' });
    if (status === 202) {
        showToast(window.I18N.tOrFallback('files.trust_confirmed', {}, 'Key trusted'), 'success');
        setTimeout(loadFilesContacts, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}

async function filesContactAcceptKeyChange(contactId) {
    const { status, data } = await filesApi('/api/mca/contacts/' + encodeURIComponent(contactId) + '/key-change/accept', { method: 'POST' });
    if (status === 202) {
        showToast(window.I18N.tOrFallback('files.key_change_accepted', {}, 'Key change accepted — confirm the new key next'), 'success');
        setTimeout(loadFilesContacts, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}

async function filesContactRejectKeyChange(contactId) {
    const { status, data } = await filesApi('/api/mca/contacts/' + encodeURIComponent(contactId) + '/key-change/reject', { method: 'POST' });
    if (status === 202) {
        showToast(window.I18N.tOrFallback('files.key_change_rejected', {}, 'Key change dismissed'), 'info');
        setTimeout(loadFilesContacts, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}

// ---- transfers archive (#125) ----------------------------------------------

async function loadFilesTransfers(force) {
    const { status, data } = await filesApi('/api/attachments?filter=' + encodeURIComponent(filesFilter) + '&limit=200');
    if (status !== 200 || !data || data.ok !== true) {
        if (status === 503) renderFilesTransfersError(window.I18N.tOrFallback('files.not_ready', {}, 'MCAttach service is not ready'));
        else renderFilesTransfersError(filesErrorCode(data));
        return;
    }
    filesTransfers = Array.isArray(data.attachments) ? data.attachments : [];
    renderFilesTransfers();
}

function renderFilesTransfersError(message) {
    const list = document.getElementById('filesArchiveList');
    if (!list) return;
    filesTransfers = [];
    list.innerHTML = '<div class="files-empty">' + escapeHtml(message) + '</div>';
}

function renderFilesTransfers() {
    const list = document.getElementById('filesArchiveList');
    if (!list) return;
    if (!filesTransfers.length) {
        list.innerHTML = '<div class="files-empty">' + escapeHtml(window.I18N.tOrFallback('files.no_transfers', {}, 'No transfers match this filter.')) + '</div>';
        return;
    }
    list.innerHTML = filesTransfers.map(function (a) {
        const dir = a.direction === 'sent' ? '↑' : '↓';
        const dirLabel = a.direction === 'sent'
            ? window.I18N.tOrFallback('files.sent', {}, 'Sent')
            : window.I18N.tOrFallback('files.received', {}, 'Received');
        const selected = a.id === filesSelectedId ? ' is-selected' : '';
        return (
            '<button type="button" class="files-transfer-item' + selected + '" onclick="selectFilesTransfer(\'' + a.id + '\')">' +
                '<span class="files-transfer-dir">' + dir + '</span>' +
                '<span class="files-transfer-name">' + escapeHtml(a.file_name || a.id) + '</span>' +
                '<span class="files-transfer-state files-state-' + escapeHtml(a.state) + '">' + escapeHtml(filesStateLabel(a.state)) + '</span>' +
                '<span class="files-transfer-size">' + escapeHtml(filesFormatBytes(a.plain_size)) + '</span>' +
                '<span class="files-transfer-date">' + escapeHtml(filesFormatDate(a.created_at)) + '</span>' +
            '</button>'
        );
    }).join('');
}

function setFilesFilter(filter) {
    filesFilter = filter;
    document.querySelectorAll('#filesFilterTabs .files-filter-tab').forEach(function (btn) {
        btn.classList.toggle('active', btn.dataset.filter === filter);
    });
    loadFilesTransfers(true);
}

// ---- detail pane (#125) ----------------------------------------------------

async function selectFilesTransfer(id) {
    filesSelectedId = id;
    document.querySelectorAll('#filesArchiveList .files-transfer-item').forEach(function (btn) {
        btn.classList.toggle('is-selected', btn.onclick && btn.getAttribute('onclick').indexOf("'" + id + "'") !== -1);
    });
    renderFilesDetail(id, false);
}

async function renderFilesDetail(id, silent) {
    const body = document.getElementById('filesDetailBody');
    if (!body) return;
    const { status, data } = await filesApi('/api/attachments/' + encodeURIComponent(id));
    if (status !== 200 || !data || data.ok !== true) {
        body.innerHTML = '<div class="files-detail-empty">' + escapeHtml(filesErrorCode(data)) + '</div>';
        return;
    }
    const a = data.attachment;
    const timeline = Array.isArray(data.timeline) ? data.timeline : [];
    const recipient = a.recipients && a.recipients.length ? a.recipients[0] : null;
    const recipientId = recipient ? (recipient.principal_id || recipient.key_id) : '';

    let actions = '';
    if (a.direction === 'received' && a.state === 'WAITING_CONSENT') {
        actions += '<button type="button" class="files-action-btn" onclick="filesAttachmentDownload(\'' + a.id + '\')">' +
            escapeHtml(window.I18N.tOrFallback('files.download', {}, 'Download')) + '</button>';
        actions += '<button type="button" class="files-action-btn is-danger" onclick="filesAttachmentReject(\'' + a.id + '\')">' +
            escapeHtml(window.I18N.tOrFallback('files.reject', {}, 'Reject')) + '</button>';
    }
    if (a.direction === 'received' && a.state === 'AVAILABLE' && a.content_available) {
        actions += '<button type="button" class="files-action-btn" onclick="filesAttachmentOpen(\'' + a.id + '\')">' +
            escapeHtml(window.I18N.tOrFallback('files.open', {}, 'Open')) + '</button>';
        if (!a.saved) {
            actions += '<button type="button" class="files-action-btn" onclick="filesAttachmentSave(\'' + a.id + '\')">' +
                escapeHtml(window.I18N.tOrFallback('files.save', {}, 'Save to Pi')) + '</button>';
        } else {
            actions += '<button type="button" class="files-action-btn is-danger" onclick="filesAttachmentDeleteLocal(\'' + a.id + '\')">' +
                escapeHtml(window.I18N.tOrFallback('files.delete_local', {}, 'Delete local copy')) + '</button>';
        }
    }
    if (a.direction === 'sent' && a.state === 'DRAFT') {
        actions += '<button type="button" class="files-action-btn is-danger" onclick="filesAttachmentCancel(\'' + a.id + '\')">' +
            escapeHtml(window.I18N.tOrFallback('files.cancel', {}, 'Cancel')) + '</button>';
    }
    if (a.direction === 'sent' && (a.state === 'SENT' || a.state === 'RECEIVED' || a.state === 'DOWNLOADED')) {
        actions += '<button type="button" class="files-action-btn is-danger" onclick="filesAttachmentRevoke(\'' + a.id + '\')">' +
            escapeHtml(window.I18N.tOrFallback('files.revoke', {}, 'Revoke')) + '</button>';
    }
    const failed = (a.direction === 'sent' && a.state.indexOf('FAILED') === 0) || a.state === 'FAILED';
    if (failed) {
        actions += '<button type="button" class="files-action-btn" onclick="filesAttachmentRetry(\'' + a.id + '\')">' +
            escapeHtml(window.I18N.tOrFallback('files.retry', {}, 'Retry')) + '</button>';
    }

    const rows = [
        ['files.detail_direction', 'Direction', a.direction === 'sent' ? window.I18N.tOrFallback('files.sent', {}, 'Sent') : window.I18N.tOrFallback('files.received', {}, 'Received')],
        ['files.detail_state', 'State', filesStateLabel(a.state)],
        ['files.detail_file', 'File', a.file_name || '—'],
        ['files.detail_type', 'Type', a.mime_type || '—'],
        ['files.detail_size', 'Size', filesFormatBytes(a.plain_size)],
        ['files.detail_recipient', 'Contact', recipientId || '—'],
        ['files.detail_created', 'Created', filesFormatDate(a.created_at)],
        ['files.detail_expires', 'Expires', filesFormatDate(a.hard_expires_at)],
    ];

    body.innerHTML =
        '<div class="files-detail-head">' +
            '<div class="files-detail-name">' + escapeHtml(a.file_name || a.id) + '</div>' +
            '<div class="files-detail-state files-state-' + escapeHtml(a.state) + '">' + escapeHtml(filesStateLabel(a.state)) + '</div>' +
        '</div>' +
        '<div class="files-detail-table">' +
            rows.map(function (r) {
                return '<div class="files-detail-row"><span class="files-detail-label">' +
                    escapeHtml(window.I18N.tOrFallback(r[0], {}, r[1])) +
                    '</span><span class="files-detail-value">' + escapeHtml(r[2]) + '</span></div>';
            }).join('') +
        '</div>' +
        '<div class="files-detail-actions">' + actions + '</div>' +
        (timeline.length
            ? '<div class="files-detail-timeline">' + timeline.map(function (e) {
                return '<div class="files-timeline-event"><span class="files-timeline-time">' +
                    escapeHtml(filesFormatDate(e.at || e.created_at)) +
                    '</span><span class="files-timeline-text">' + escapeHtml(e.event || e.state || '') + '</span></div>';
            }).join('') + '</div>'
            : '');
}

// ---- attachment lifecycle actions ------------------------------------------

async function filesAttachmentCommand(id, action, successMessage) {
    const { status, data } = await filesApi('/api/attachments/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
    if (status === 202) {
        showToast(successMessage, 'info');
        setTimeout(function () { loadFilesTransfers(true); if (filesSelectedId === id) renderFilesDetail(id, true); }, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}

function filesAttachmentDownload(id) {
    filesAttachmentCommand(id, 'download', window.I18N.tOrFallback('files.download_started', {}, 'Download started'));
}
function filesAttachmentReject(id) {
    filesAttachmentCommand(id, 'reject', window.I18N.tOrFallback('files.rejected', {}, 'Transfer rejected'));
}
function filesAttachmentSave(id) {
    filesAttachmentCommand(id, 'save', window.I18N.tOrFallback('files.saving', {}, 'Saving to Pi…'));
}
function filesAttachmentCancel(id) {
    filesAttachmentCommand(id, 'cancel', window.I18N.tOrFallback('files.cancelled', {}, 'Transfer cancelled'));
}
function filesAttachmentRevoke(id) {
    filesAttachmentCommand(id, 'revoke', window.I18N.tOrFallback('files.revoked', {}, 'Transfer revoked'));
}
function filesAttachmentRetry(id) {
    filesAttachmentCommand(id, 'retry', window.I18N.tOrFallback('files.retrying', {}, 'Retrying…'));
}
async function filesAttachmentDeleteLocal(id) {
    const { status, data } = await filesApi('/api/attachments/' + encodeURIComponent(id) + '/local-content', { method: 'DELETE' });
    if (status === 202) {
        showToast(window.I18N.tOrFallback('files.local_deleted', {}, 'Local copy deleted'), 'info');
        setTimeout(function () { loadFilesTransfers(true); if (filesSelectedId === id) renderFilesDetail(id, true); }, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}
function filesAttachmentOpen(id) {
    // The verified plaintext streams inline (images) or as an attachment.
    window.open('/api/attachments/' + encodeURIComponent(id) + '/content', '_blank', 'noopener');
}

// ---- send-file dialog (#126) -----------------------------------------------

async function openFilesSendDialog() {
    const trusted = filesContacts.filter(function (c) { return c.status === 'trusted'; });
    const { status, data } = await filesApi('/api/mca/providers');
    let providers = (status === 200 && data && data.ok) ? (data.providers || []) : [];
    providers = providers.filter(function (p) { return p.upload_allowed && p.enabled; });

    const contactOptions = trusted.length
        ? trusted.map(function (c) {
            return '<option value="' + escapeHtml(c.contact_id) + '">' + escapeHtml(c.contact_id) + '</option>';
        }).join('')
        : '<option value="">' + escapeHtml(window.I18N.tOrFallback('files.no_trusted_contacts', {}, 'No trusted contacts')) + '</option>';

    const providerOptions = providers.length
        ? providers.map(function (p) {
            return '<option value="' + escapeHtml(p.provider_id) + '"' + (p.is_default ? ' selected' : '') + '>' +
                escapeHtml(p.display_name || p.origin || p.provider_id) + '</option>';
        }).join('')
        : '<option value="">' + escapeHtml(window.I18N.tOrFallback('files.no_providers', {}, 'No upload providers configured')) + '</option>';

    const html =
        '<div class="files-modal-backdrop" onclick="filesModalBackdropClose(event)">' +
            '<div class="files-modal" role="dialog" aria-modal="true">' +
                '<div class="files-modal-header">' +
                    '<h3 class="files-modal-title">📤 ' + escapeHtml(window.I18N.tOrFallback('files.send_file', {}, 'Send file')) + '</h3>' +
                    '<button type="button" class="files-modal-close" onclick="closeFilesSendDialog()">×</button>' +
                '</div>' +
                '<div class="files-modal-body">' +
                    '<label class="files-field">' +
                        '<span class="files-field-label">' + escapeHtml(window.I18N.tOrFallback('files.send_recipient', {}, 'Recipient')) + '</span>' +
                        '<select id="filesSendRecipient">' + contactOptions + '</select>' +
                    '</label>' +
                    '<label class="files-field">' +
                        '<span class="files-field-label">' + escapeHtml(window.I18N.tOrFallback('files.send_provider', {}, 'Relay provider')) + '</span>' +
                        '<select id="filesSendProvider">' + providerOptions + '</select>' +
                    '</label>' +
                    '<label class="files-field">' +
                        '<span class="files-field-label">' + escapeHtml(window.I18N.tOrFallback('files.send_file_label', {}, 'File')) + '</span>' +
                        '<input type="file" id="filesSendFile" />' +
                    '</label>' +
                    '<label class="files-field">' +
                        '<span class="files-field-label">' + escapeHtml(window.I18N.tOrFallback('files.send_expiry', {}, 'Expiry')) + '</span>' +
                        '<select id="filesSendExpiry">' +
                            '<option value="3600">' + escapeHtml(window.I18N.tOrFallback('files.expiry_1h', {}, '1 hour')) + '</option>' +
                            '<option value="21600">' + escapeHtml(window.I18N.tOrFallback('files.expiry_6h', {}, '6 hours')) + '</option>' +
                            '<option value="86400" selected>' + escapeHtml(window.I18N.tOrFallback('files.expiry_24h', {}, '24 hours')) + '</option>' +
                            '<option value="604800">' + escapeHtml(window.I18N.tOrFallback('files.expiry_7d', {}, '7 days')) + '</option>' +
                        '</select>' +
                    '</label>' +
                    '<label class="files-field">' +
                        '<span class="files-field-label">' + escapeHtml(window.I18N.tOrFallback('files.send_comment', {}, 'Comment (optional)')) + '</span>' +
                        '<input type="text" id="filesSendComment" maxlength="280" />' +
                    '</label>' +
                '</div>' +
                '<div class="files-modal-footer">' +
                    '<button type="button" class="files-modal-cancel" onclick="closeFilesSendDialog()">' +
                        escapeHtml(window.I18N.tOrFallback('common.cancel', {}, 'Cancel')) + '</button>' +
                    '<button type="button" class="files-modal-submit" onclick="submitFilesSend()">' +
                        escapeHtml(window.I18N.tOrFallback('files.send_submit', {}, 'Send')) + '</button>' +
                '</div>' +
            '</div>' +
        '</div>';

    filesDialogEl = document.createElement('div');
    filesDialogEl.className = 'files-dialog-root';
    filesDialogEl.innerHTML = html;
    document.body.appendChild(filesDialogEl);
}

function closeFilesSendDialog() {
    if (filesDialogEl) { filesDialogEl.remove(); filesDialogEl = null; }
}

function filesModalBackdropClose(event) {
    if (event.target.classList.contains('files-modal-backdrop')) {
        closeFilesSendDialog();
    }
}

async function submitFilesSend() {
    const recipient = document.getElementById('filesSendRecipient');
    const provider = document.getElementById('filesSendProvider');
    const fileInput = document.getElementById('filesSendFile');
    const expiry = document.getElementById('filesSendExpiry');
    const comment = document.getElementById('filesSendComment');

    if (!recipient || !recipient.value) {
        showToast(window.I18N.tOrFallback('files.err_no_recipient', {}, 'Choose a trusted contact'), 'error');
        return;
    }
    if (!fileInput || !fileInput.files || !fileInput.files.length) {
        showToast(window.I18N.tOrFallback('files.err_no_file', {}, 'Choose a file'), 'error');
        return;
    }
    const file = fileInput.files[0];
    if (file.size > FILES_MAX_SEND_BYTES) {
        showToast(window.I18N.tOrFallback('files.err_file_too_large', {}, 'File exceeds the 5 MiB cap'), 'error');
        return;
    }

    let clientRequestId;
    try {
        clientRequestId = (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : 'req-' + Date.now();
    } catch (_) {
        clientRequestId = 'req-' + Date.now();
    }

    const metadata = {
        client_request_id: clientRequestId,
        recipient: { source_address: recipient.value },
        hard_ttl_seconds: parseInt(expiry.value, 10) || 86400,
        comment: (comment && comment.value.trim()) ? comment.value.trim() : null,
    };
    if (provider && provider.value) metadata.provider_id = provider.value;

    const form = new FormData();
    form.append('metadata', JSON.stringify(metadata));
    form.append('file', file);

    const progressId = showProgressNotification(window.I18N.tOrFallback('files.sending', {}, 'Sending file…'));
    const { status, data } = await filesApi('/api/attachments', { method: 'POST', body: form });

    if (status === 202 && data && data.ok) {
        updateNotification(progressId, window.I18N.tOrFallback('files.send_queued', {}, 'Transfer created'), 'success');
        closeFilesSendDialog();
        filesFilter = 'all';
        loadFilesTransfers(true);
        if (data.attachment_id) selectFilesTransfer(data.attachment_id);
    } else {
        updateNotification(progressId, filesErrorCode(data), 'error');
    }
}

// ---- relay provider settings (#127) ----------------------------------------

async function openFilesProviderSettings() {
    const html =
        '<div class="files-modal-backdrop" onclick="filesModalBackdropClose(event)">' +
            '<div class="files-modal files-modal-wide" role="dialog" aria-modal="true">' +
                '<div class="files-modal-header">' +
                    '<h3 class="files-modal-title">⚙️ ' + escapeHtml(window.I18N.tOrFallback('files.providers', {}, 'Relay providers')) + '</h3>' +
                    '<button type="button" class="files-modal-close" onclick="closeFilesProviderSettings()">×</button>' +
                '</div>' +
                '<div class="files-modal-body">' +
                    '<div id="filesProvidersList"><div class="files-empty">' +
                        escapeHtml(window.I18N.tOrFallback('files.loading_providers', {}, 'Loading providers…')) +
                    '</div></div>' +
                    '<div class="files-provider-add">' +
                        '<h4 class="files-provider-add-title">' + escapeHtml(window.I18N.tOrFallback('files.add_provider', {}, 'Add provider')) + '</h4>' +
                        '<input type="url" id="filesProviderOrigin" placeholder="https://relay.example.com" />' +
                        '<button type="button" class="files-action-btn" onclick="filesProviderProbe()">' +
                            escapeHtml(window.I18N.tOrFallback('files.probe', {}, 'Probe')) + '</button>' +
                        '<div id="filesProviderProbeResult"></div>' +
                    '</div>' +
                '</div>' +
                '<div class="files-modal-footer">' +
                    '<button type="button" class="files-modal-cancel" onclick="closeFilesProviderSettings()">' +
                        escapeHtml(window.I18N.tOrFallback('common.close', {}, 'Close')) + '</button>' +
                '</div>' +
            '</div>' +
        '</div>';

    filesDialogEl = document.createElement('div');
    filesDialogEl.className = 'files-dialog-root';
    filesDialogEl.innerHTML = html;
    document.body.appendChild(filesDialogEl);
    renderFilesProviders();
}

function closeFilesProviderSettings() {
    if (filesDialogEl) { filesDialogEl.remove(); filesDialogEl = null; }
}

async function renderFilesProviders() {
    const list = document.getElementById('filesProvidersList');
    if (!list) return;
    const { status, data } = await filesApi('/api/mca/providers');
    if (status !== 200 || !data || data.ok !== true) {
        list.innerHTML = '<div class="files-empty">' + escapeHtml(filesErrorCode(data)) + '</div>';
        return;
    }
    const providers = Array.isArray(data.providers) ? data.providers : [];
    if (!providers.length) {
        list.innerHTML = '<div class="files-empty">' + escapeHtml(window.I18N.tOrFallback('files.no_providers', {}, 'No Relay providers configured.')) + '</div>';
        return;
    }
    list.innerHTML = providers.map(function (p) {
        const stateLabel = p.state || 'unknown';
        return (
            '<div class="files-provider-item">' +
                '<div class="files-provider-head">' +
                    '<span class="files-provider-name">' + escapeHtml(p.display_name || p.origin || p.provider_id) +
                        (p.is_default ? ' <span class="files-provider-default">★</span>' : '') + '</span>' +
                    '<span class="files-provider-state files-state-' + escapeHtml(stateLabel) + '">' + escapeHtml(stateLabel) + '</span>' +
                '</div>' +
                '<div class="files-provider-meta">' + escapeHtml(p.origin || '') + '</div>' +
                '<div class="files-provider-actions">' +
                    (p.is_default ? '' :
                        '<button type="button" class="files-action-btn" onclick="filesProviderSetDefault(\'' + escapeHtml(p.provider_id) + '\')">' +
                            escapeHtml(window.I18N.tOrFallback('files.set_default', {}, 'Set default')) + '</button>') +
                    '<button type="button" class="files-action-btn" onclick="filesProviderToggleEnabled(\'' + escapeHtml(p.provider_id) + '\', ' + (p.enabled ? 'false' : 'true') + ')">' +
                        escapeHtml(p.enabled ? window.I18N.tOrFallback('files.disable', {}, 'Disable') : window.I18N.tOrFallback('files.enable', {}, 'Enable')) + '</button>' +
                    '<button type="button" class="files-action-btn" onclick="filesProviderCheck(\'' + escapeHtml(p.provider_id) + '\')">' +
                        escapeHtml(window.I18N.tOrFallback('files.check', {}, 'Check')) + '</button>' +
                    '<button type="button" class="files-action-btn is-danger" onclick="filesProviderRemove(\'' + escapeHtml(p.provider_id) + '\')">' +
                        escapeHtml(window.I18N.tOrFallback('files.remove', {}, 'Remove')) + '</button>' +
                '</div>' +
                '<div class="files-provider-token">' +
                    '<input type="password" id="filesToken-' + escapeHtml(p.provider_id) + '" placeholder="' +
                        escapeHtml(p.upload_token_configured ? window.I18N.tOrFallback('files.token_set', {}, 'Token set (enter to replace)') : window.I18N.tOrFallback('files.token_placeholder', {}, 'Upload token')) + '" />' +
                    '<button type="button" class="files-action-btn" onclick="filesProviderSetToken(\'' + escapeHtml(p.provider_id) + '\')">' +
                        escapeHtml(window.I18N.tOrFallback('files.save_token', {}, 'Save token')) + '</button>' +
                    (p.upload_token_configured ?
                        '<button type="button" class="files-action-btn" onclick="filesProviderClearToken(\'' + escapeHtml(p.provider_id) + '\')">' +
                            escapeHtml(window.I18N.tOrFallback('files.clear_token', {}, 'Clear')) + '</button>' : '') +
                '</div>' +
            '</div>'
        );
    }).join('');
}

async function filesProviderCommand(url, method, body, successMessage) {
    const options = { method: method };
    if (body !== undefined) {
        options.headers = { 'Content-Type': 'application/json' };
        options.body = JSON.stringify(body);
    }
    const { status, data } = await filesApi(url, options);
    if (status === 202) {
        showToast(successMessage, 'info');
        setTimeout(renderFilesProviders, 600);
    } else {
        showToast(filesErrorCode(data), 'error');
    }
}

function filesProviderSetDefault(id) {
    filesProviderCommand('/api/mca/providers/' + encodeURIComponent(id) + '/default', 'POST', undefined,
        window.I18N.tOrFallback('files.default_set', {}, 'Default provider updated'));
}
function filesProviderToggleEnabled(id, enabled) {
    filesProviderCommand('/api/mca/providers/' + encodeURIComponent(id), 'PATCH', { enabled: enabled },
        window.I18N.tOrFallback('files.provider_updated', {}, 'Provider updated'));
}
function filesProviderCheck(id) {
    filesProviderCommand('/api/mca/providers/' + encodeURIComponent(id) + '/check', 'POST', undefined,
        window.I18N.tOrFallback('files.check_started', {}, 'Check started'));
}
function filesProviderRemove(id) {
    filesProviderCommand('/api/mca/providers/' + encodeURIComponent(id), 'DELETE', undefined,
        window.I18N.tOrFallback('files.provider_removed', {}, 'Provider removed or disabled'));
}
function filesProviderSetToken(id) {
    const input = document.getElementById('filesToken-' + id);
    if (!input || !input.value.trim()) {
        showToast(window.I18N.tOrFallback('files.err_no_token', {}, 'Enter a token'), 'error');
        return;
    }
    filesProviderCommand('/api/mca/providers/' + encodeURIComponent(id) + '/upload-token', 'PUT', { upload_token: input.value.trim() },
        window.I18N.tOrFallback('files.token_saved', {}, 'Upload token saved'));
}
function filesProviderClearToken(id) {
    filesProviderCommand('/api/mca/providers/' + encodeURIComponent(id) + '/upload-token', 'DELETE', undefined,
        window.I18N.tOrFallback('files.token_cleared', {}, 'Upload token cleared'));
}

async function filesProviderProbe() {
    const origin = document.getElementById('filesProviderOrigin');
    const result = document.getElementById('filesProviderProbeResult');
    if (!origin || !origin.value.trim()) {
        showToast(window.I18N.tOrFallback('files.err_no_origin', {}, 'Enter a provider URL'), 'error');
        return;
    }
    const progressId = showProgressNotification(window.I18N.tOrFallback('files.probing', {}, 'Probing provider…'));
    const { status, data } = await filesApi('/api/mca/providers/probe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: origin.value.trim() }),
    });
    if (status !== 202 || !data || !data.command_id) {
        updateNotification(progressId, filesErrorCode(data), 'error');
        return;
    }
    const cmd = await filesPollCommand(data.command_id);
    if (!cmd) {
        updateNotification(progressId, window.I18N.tOrFallback('files.probe_timeout', {}, 'Probe timed out'), 'error');
        return;
    }
    if (cmd.status === 'failed') {
        updateNotification(progressId, filesErrorCode({ error_code: cmd.error_code, error: cmd.error_code }), 'error');
        return;
    }
    updateNotification(progressId, window.I18N.tOrFallback('files.probe_ok', {}, 'Provider reached'), 'success');
    filesLastProbe = cmd.result || {};
    renderFilesProbeConfirm(filesLastProbe, result);
}

function renderFilesProbeConfirm(probe, resultEl) {
    if (!resultEl) return;
    const fp = probe.service_key_fingerprint || '';
    resultEl.innerHTML =
        '<div class="files-provider-probe">' +
            '<div class="files-provider-probe-line">' + escapeHtml(window.I18N.tOrFallback('files.probe_origin', {}, 'Origin')) + ': ' + escapeHtml(probe.origin || '') + '</div>' +
            '<div class="files-provider-probe-line">' + escapeHtml(window.I18N.tOrFallback('files.probe_fingerprint', {}, 'Service key fingerprint')) + ': ' +
                '<code>' + escapeHtml(fp) + '</code></div>' +
            '<div class="files-provider-probe-line">' + escapeHtml(window.I18N.tOrFallback('files.probe_confirm_hint', {}, 'Verify the fingerprint against your provider, then name and register it.')) + '</div>' +
            '<input type="text" id="filesProviderName" placeholder="' + escapeHtml(window.I18N.tOrFallback('files.provider_name', {}, 'Provider name')) + '" />' +
            '<button type="button" class="files-action-btn" onclick="filesProviderRegister()">' +
                escapeHtml(window.I18N.tOrFallback('files.register', {}, 'Confirm & register')) + '</button>' +
        '</div>';
}

async function filesProviderRegister() {
    const probe = filesLastProbe;
    if (!probe || !probe.probe_id) return;
    const nameInput = document.getElementById('filesProviderName');
    const displayName = (nameInput && nameInput.value.trim()) ? nameInput.value.trim() : (probe.origin || '');
    const progressId = showProgressNotification(window.I18N.tOrFallback('files.registering', {}, 'Registering provider…'));
    const { status, data } = await filesApi('/api/mca/providers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            probe_id: probe.probe_id,
            fingerprint_confirmation: probe.service_key_fingerprint || '',
            display_name: displayName,
            policy: { kind: 'own', upload_allowed: true, download_allowed: true },
        }),
    });
    if (status !== 202 || !data || !data.command_id) {
        updateNotification(progressId, filesErrorCode(data), 'error');
        return;
    }
    const cmd = await filesPollCommand(data.command_id);
    if (cmd && cmd.status === 'succeeded') {
        updateNotification(progressId, window.I18N.tOrFallback('files.provider_registered', {}, 'Provider registered'), 'success');
        filesLastProbe = null;
        const resultEl = document.getElementById('filesProviderProbeResult');
        if (resultEl) resultEl.innerHTML = '';
        const originEl = document.getElementById('filesProviderOrigin');
        if (originEl) originEl.value = '';
        renderFilesProviders();
    } else {
        updateNotification(progressId, filesErrorCode({ error_code: cmd && cmd.error_code, error: cmd && cmd.error_code }), 'error');
    }
}

// ---- polling (#128) --------------------------------------------------------

function startFilesPolling() {
    if (filesPollTimer) return;
    filesPollTimer = setInterval(filesPollTick, FILES_POLL_MS);
}

function stopFilesPolling() {
    if (filesPollTimer) {
        clearInterval(filesPollTimer);
        filesPollTimer = null;
    }
}

function filesPollTick() {
    if (typeof currentMainTab !== 'undefined' && currentMainTab !== 'files') {
        stopFilesPolling();
        return;
    }
    loadFilesContacts();
    loadFilesTransfers(false);
    if (filesSelectedId) renderFilesDetail(filesSelectedId, true);
}

// Expose the entry points referenced from static markup (onclick handlers) and
// chat.js's `switchMainTab` for clarity, alongside the implicit top-level
// globals this plain script already creates.
window.openFilesWorkspace = openFilesWorkspace;
window.closeFilesWorkspace = closeFilesWorkspace;
window.loadFilesWorkspace = loadFilesWorkspace;
window.setFilesFilter = setFilesFilter;
window.openFilesSendDialog = openFilesSendDialog;
window.closeFilesSendDialog = closeFilesSendDialog;
window.openFilesProviderSettings = openFilesProviderSettings;
window.closeFilesProviderSettings = closeFilesProviderSettings;
