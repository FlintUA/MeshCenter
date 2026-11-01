// ============================================================
// MCAttach vault UI (MCA-VAULT PR 5, ADR-0013). One module for everything the owner sees of the vault:
//   - the lock chip in the dock (hidden while there is no vault)
//   - the banner above the Files transfer list (locked / interrupted job / the plain backup that still exists)
//   - the unlock dialog (a centred dialog on a desktop, a bottom sheet on a phone)
//   - the vault card in System: enable (wizard), Lock now, change passphrase, new recovery key, disable
// Secrets: a passphrase or recovery key lives only in its <input> and in the one request that carries it. It is never
// stored in a variable that outlives the handler, never written to browser storage, never put in a URL, a log or a toast,
// and the field is cleared as soon as the request has been sent. Names and comments of transfers never appear here.
// The pure parts (strength hint, view models, error keys) are unit-tested in tests/frontend/test_vault_ui.mjs.
// ============================================================
(function (root) {
    'use strict';

    // ---- pure: the passphrase strength hint (must match meshsrv/attachments/vault/passphrase.py; the server decides) ----
    var MIN_LENGTH = 12;
    var COMMON = [
        'passwordpassword', 'password1234', '123456789012', 'qwertyuiop12', 'qwertyuiopasdf', 'iloveyouiloveyou',
        'administrator', 'letmeinletmein', 'welcomewelcome', '1234567890123', '123456789abc', 'abcdefghijkl',
        'meshcentermeshcenter', 'meshtasticmeshtastic', 'changemechangeme'
    ];

    function assessPassphrase(passphrase) {
        var text = String(passphrase || '');
        if (typeof text.normalize === 'function') text = text.normalize('NFKC');
        var chars = Array.from(text);
        var length = chars.length;
        if (length < MIN_LENGTH) return { ok: false, level: 'too_short', length: length };
        var distinct = new Set(chars).size;
        if (COMMON.indexOf(text.toLowerCase().split(' ').join('')) !== -1 || distinct <= 3) return { ok: false, level: 'common', length: length };
        var classes = 0;
        if (text !== text.toUpperCase()) classes++;          // has a lower-case letter
        if (text !== text.toLowerCase()) classes++;          // has an upper-case letter
        if (/[0-9]/.test(text)) classes++;
        if (/[^\p{L}\p{N}]/u.test(text)) classes++;
        var words = text.split(/\s+/).filter(Boolean).length;
        var level;
        if (length >= 20 || (length >= 16 && (classes >= 3 || words >= 3))) level = 'good';
        else if (length >= 14 && (classes >= 2 || words >= 2)) level = 'fair';
        else level = 'weak';
        return { ok: true, level: level, length: length };
    }

    // ---- pure: view models -------------------------------------------------------------------------------------
    // The chip: nothing while there is no vault; locked / unlocked / a job is running or was interrupted.
    function chipModel(status) {
        if (!status || status.ok === false || status.state === 'disabled' || !status.state) return null;
        if (status.state === 'migrating' || status.progress) return { state: 'migrating', icon: '⏳', labelKey: 'vault.chip_job', tone: 'warn' };
        if (status.locked || status.state === 'locked') return { state: 'locked', icon: '🔒', labelKey: 'vault.chip_locked', tone: 'warn' };
        return { state: 'unlocked', icon: '🔓', labelKey: 'vault.chip_unlocked', tone: 'ok' };
    }

    // The Files banner: what to say above the transfer list. Each entry is one line with at most one action.
    function bannerModel(status) {
        var lines = [];
        var backups = status && Array.isArray(status.plain_backups) ? status.plain_backups : [];
        if (!status || status.ok === false || status.state === 'disabled' || !status.state) {
            // no vault, but a plain backup folder may still be there (a vault that was disabled, a folder copied back)
            backups.forEach(function (b) { lines.push({ kind: 'backup', tone: 'danger', backup: b }); });
            return lines;
        }
        if (status.state === 'migrating') {
            lines.push({ kind: 'job', tone: 'warn', pending: status.migration_pending || null, running: Boolean(status.progress) });
        } else if (status.locked || status.state === 'locked') {
            lines.push({ kind: 'locked', tone: 'warn', items: Number(status.locked_items) || 0, offers: Number(status.waiting_offers) || 0 });
        }
        backups.forEach(function (b) { lines.push({ kind: 'backup', tone: 'danger', backup: b }); });
        return lines;
    }

    // An API error code as an i18n key under vault.error.* (unknown codes get the generic line)
    var ERROR_CODES = [
        'unlock_failed', 'invalid_recovery_key', 'unlock_rate_limited', 'insufficient_memory', 'vault_busy', 'vault_locked',
        'passphrase_too_weak', 'passphrase_required', 'vault_already_enabled', 'vault_not_enabled', 'transfers_in_progress',
        'acknowledgement_required', 'vault_corrupt', 'vault_not_available', 'nothing_to_resume', 'command_queue_full',
        'recovery_key_not_available', 'vault_handoff_expired', 'invalid_backup_name', 'not_ready'
    ];
    function errorKey(code) {
        return 'vault.error.' + (ERROR_CODES.indexOf(code) !== -1 ? code : 'generic');
    }

    // Two group positions (1-based) of the 13-group recovery key to type back, chosen by `random` (a function returning [0,1)).
    function recoveryGroups(text) {
        return String(text || '').trim().split('-').filter(Boolean);
    }
    function pickTypeBack(count, random) {
        var rnd = typeof random === 'function' ? random : Math.random;
        var first = Math.floor(rnd() * count);
        var second = Math.floor(rnd() * (count - 1));
        if (second >= first) second++;
        return first < second ? [first + 1, second + 1] : [second + 1, first + 1];
    }
    function typeBackMatches(groups, positions, typed) {
        for (var i = 0; i < positions.length; i++) {
            var want = String(groups[positions[i] - 1] || '').toUpperCase();
            var got = String((typed && typed[i]) || '').replace(/[\s-]/g, '').toUpperCase();
            if (!want || want !== got) return false;
        }
        return true;
    }

    var api = {
        MIN_LENGTH: MIN_LENGTH, COMMON: COMMON, ERROR_CODES: ERROR_CODES,
        assessPassphrase: assessPassphrase, chipModel: chipModel, bannerModel: bannerModel, errorKey: errorKey,
        recoveryGroups: recoveryGroups, pickTypeBack: pickTypeBack, typeBackMatches: typeBackMatches
    };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;

    var doc = root && root.document;
    if (!doc || typeof doc.addEventListener !== 'function' || typeof doc.createElement !== 'function') {
        if (root) root.MCVault = api;
        return;
    }

    // ---- DOM glue ----------------------------------------------------------------------------------------------
    var status = null;               // the last /api/mca/vault answer
    var pollTimer = null;
    var lastSignature = '';
    var sheet = null;                // the open dialog / sheet, if any
    var busy = false;                // a request or job is running: Esc and the backdrop do not close the sheet

    function tr(key, params, fallback) {
        var i18n = root.I18N;
        var fb = fallback === undefined ? key : fallback;
        if (i18n && typeof i18n.tOrFallback === 'function') {
            var found = i18n.tOrFallback(key, params || {}, null);
            if (found !== null && found !== undefined) return found;       // translated and filled in
        }
        var text = fb;                                                    // not loaded (yet) or no such key: the English fallback
        if (params) Object.keys(params).forEach(function (k) { text = text.split('{' + k + '}').join(String(params[k])); });
        return text;
    }

    function esc(value) {
        return String(value === null || value === undefined ? '' : value).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }

    function size(bytes) {
        return root.MCSize ? root.MCSize.format(bytes) : String(bytes) + ' B';
    }

    function toast(message, type, options) {
        if (typeof root.showToast === 'function') root.showToast(message, type || 'info', options);
    }

    function call(method, url, body) {
        var init = { method: method, headers: {}, credentials: 'same-origin' };
        if (body !== undefined) {
            init.headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(body);
        }
        return root.fetch(url, init).then(function (response) {
            return response.json().catch(function () { return {}; }).then(function (data) { return { status: response.status, data: data || {} }; });
        }, function () {
            return { status: 0, data: { ok: false, error_code: 'network_error' } };
        });
    }

    function errorText(result) {
        var data = result && result.data ? result.data : {};
        var code = data.error_code || '';
        if (code === 'network_error') return tr('files.error.network_error', null, 'Network error');
        var text = tr(errorKey(code), { seconds: data.retry_in_s || 0, count: data.count || 0 }, '');
        if (!text) text = tr('vault.error.generic', null, 'The vault could not do that');
        if (code === 'insufficient_memory' && data.error && data.error !== code) return text + ' (' + data.error + ')';
        return text;
    }

    // ---- status -------------------------------------------------------------------------------------------------
    function isLocked() { return Boolean(status && status.locked); }
    function isEnabled() { return Boolean(status && status.enabled); }

    function refresh() {
        return call('GET', '/api/mca/vault').then(function (result) {
            if (result.status === 200 && result.data && result.data.ok) {
                setStatus(result.data);
            } else if (result.status === 503 || result.status === 404) {
                setStatus(null);                       // MCAttach is not running: there is nothing to show
            }                                           // 401 / network: keep what is on screen
            return status;
        });
    }

    function setStatus(next) {
        var before = lastSignature;
        status = next;
        var signature = next ? [next.state, Boolean(next.locked), (next.plain_backups || []).map(function (b) { return b.name; }).join(','), next.recovery_key_pending ? 1 : 0].join('|') : 'none';
        lastSignature = signature;
        renderAll();
        schedulePoll();
        if (before !== signature && before !== '') {
            try { root.dispatchEvent(new root.CustomEvent('mcvault:change', { detail: { locked: isLocked(), enabled: isEnabled() } })); } catch (e) { /* old browser */ }
        }
    }

    function schedulePoll() {
        if (pollTimer) root.clearTimeout(pollTimer);
        var fast = Boolean(status && (status.progress || status.state === 'migrating'));
        pollTimer = root.setTimeout(function () {
            if (doc.hidden && !fast) { schedulePoll(); return; }
            refresh();
        }, fast ? 1500 : 20000);
    }

    // ---- chip, banner, card --------------------------------------------------------------------------------------
    function renderChip() {
        var chip = doc.getElementById('dockVaultChip');
        if (!chip) return;
        var model = chipModel(status);
        if (!model) { chip.hidden = true; return; }
        chip.hidden = false;
        chip.className = 'dock-vault-chip is-' + model.state;
        var label = tr(model.labelKey, null, model.state);
        chip.setAttribute('data-vault-state', model.state);
        chip.setAttribute('title', tr('vault.chip_title_' + model.state, null, label));
        chip.setAttribute('aria-label', tr('vault.chip_title_' + model.state, null, label));
        chip.innerHTML = '<span class="dock-vault-icon" aria-hidden="true">' + model.icon + '</span><span class="dock-vault-label">' + esc(label) + '</span>';
    }

    function backupLine(b) {
        var when = b.created_at ? new Date(b.created_at * 1000).toLocaleString() : '';
        if (b.encrypted) {
            return tr('vault.backup_encrypted_warning', { folder: b.name, size: size(b.bytes), when: when },
                'A backup of your encrypted MCAttach data still exists: {folder}, {size}, made {when}. It needs the passphrase to open. Delete it when you no longer need it.');
        }
        return tr('vault.backup_warning', { folder: b.name, size: size(b.bytes), when: when },
            'A plain backup of your MCAttach data still exists: {folder}, {size}, made {when}. Anyone with access to this device can read it.');
    }

    function backupBannerHtml(b) {
        var tone = b.encrypted ? 'is-warn' : 'is-danger';
        return '<div class="vault-banner ' + tone + '" role="' + (b.encrypted ? 'status' : 'alert') + '"><span class="vault-banner-text">' + (b.encrypted ? '\uD83D\uDCBE ' : '\u26A0\uFE0F ') + esc(backupLine(b)) + '</span>' +
            '<button type="button" class="vault-banner-btn ' + tone + '" data-vault-action="delete-backup" data-backup="' + esc(b.name) + '" data-bytes="' + esc(b.bytes) + '"' + (b.encrypted ? ' data-encrypted="1"' : '') + '>' +
            esc(b.encrypted ? tr('vault.backup_delete_enc', null, 'Delete the backup') : tr('vault.backup_delete', null, 'Delete the plain backup')) + '</button></div>';
    }

    // FX1: the one line in the header of Files: "Files are encrypted", or "locked" with the way to unlock
    function renderLines() {
        var hosts = doc.querySelectorAll ? doc.querySelectorAll('[data-vault-line]') : [];
        var model = chipModel(status);
        Array.prototype.forEach.call(hosts, function (host) {
            if (!model) { host.hidden = true; host.innerHTML = ''; host.className = 'files-vault-line'; return; }
            host.hidden = false;
            var locked = model.state === 'locked';
            host.className = 'files-vault-line' + (locked ? ' is-locked' : '');
            var text = locked ? tr('vault.line_locked', null, 'Files are encrypted and locked')
                : (model.state === 'migrating' ? tr('vault.chip_title_migrating', null, 'A vault job is running or was interrupted') : tr('vault.line_encrypted', null, 'Files are encrypted'));
            host.innerHTML = '<span class="vault-line-text">🔒 ' + esc(text) + '</span>' +
                (locked ? '<button type="button" class="vault-banner-btn" data-vault-action="unlock">' + esc(tr('vault.unlock', null, 'Unlock')) + '</button>' : '');
        });
    }

    function renderBanner() {
        var hosts = doc.querySelectorAll ? doc.querySelectorAll('[data-vault-banner]') : [];
        var lines = bannerModel(status);
        Array.prototype.forEach.call(hosts, function (host) {
            // a host marked "backups" shows only the plain-backup warning and the interrupted job: the line above it says "locked"
            var only = host.getAttribute && host.getAttribute('data-vault-banner') === 'backups';
            var mine = only ? lines.filter(function (line) { return line.kind !== 'locked'; }) : lines;
            if (!mine.length) { host.hidden = true; host.innerHTML = ''; return; }
            host.hidden = false;
            host.innerHTML = mine.map(function (line) {
                if (line.kind === 'locked') {
                    var text = tr('vault.banner_locked', { count: line.items }, 'MCAttach is locked. {count} transfers are hidden until you unlock it.');
                    if (line.offers > 0) text += ' ' + tr('vault.banner_offers', { count: line.offers }, '{count} file offers are waiting.');
                    return '<div class="vault-banner is-warn" role="status"><span class="vault-banner-text">🔒 ' + esc(text) + '</span>' +
                        '<button type="button" class="vault-banner-btn" data-vault-action="unlock">' + esc(tr('vault.unlock', null, 'Unlock')) + '</button></div>';
                }
                if (line.kind === 'job') {
                    var jobText = tr(line.running ? 'vault.banner_job_running' : 'vault.banner_job_pending', null,
                        line.running ? 'A vault job is running.' : 'A vault job was interrupted. MCAttach stays locked until it is finished.');
                    var action = line.running ? '' : '<button type="button" class="vault-banner-btn" data-vault-action="resume">' + esc(tr('vault.resume', null, 'Finish the job')) + '</button>';
                    return '<div class="vault-banner is-warn" role="status"><span class="vault-banner-text">⏳ ' + esc(jobText) + '</span>' + action + '</div>';
                }
                return backupBannerHtml(line.backup);
            }).join('');
        });
    }

    function renderCard() {
        var body = doc.getElementById('vaultCardBody');
        if (!body) return;
        if (!status) { body.innerHTML = '<div class="settings-card-text settings-card-note">' + esc(tr('vault.card_unavailable', null, 'MCAttach is not running.')) + '</div>'; return; }
        var html = [];
        var state = status.state;
        var stateText = {
            disabled: tr('vault.state_disabled', null, 'Off'), locked: tr('vault.state_locked', null, 'On, locked'),
            unlocked: tr('vault.state_unlocked', null, 'On, unlocked'), migrating: tr('vault.state_job', null, 'A vault job is running or was interrupted')
        }[state] || state;
        html.push('<div class="vault-card-state"><strong>' + esc(tr('vault.card_state', null, 'State')) + ':</strong> <span data-vault-state-text>' + esc(stateText) + '</span></div>');
        html.push('<div class="settings-card-text">' + esc(tr('vault.card_description', null,
            'Encrypts the files, names, comments and keys MCAttach stores on this device with a passphrase only you know. MCAttach is locked after every restart until you unlock it.')) + '</div>');
        html.push('<div class="settings-card-text settings-card-note vault-note">🖼️ ' + esc(tr('vault.note_gallery', null,
            'The Media gallery is not covered: camera pictures and their thumbnails stay as they are. Only the MCAttach copy of a picture you send is inside the vault.')) + '</div>');
        if (status.progress) {
            var p = status.progress;
            html.push('<div class="vault-progress" role="status">' + esc(tr('vault.phase.' + p.phase, null, p.phase)) + ' ' + esc(p.done) + '/' + esc(p.total) + '</div>');
        }
        if (state !== 'disabled') {
            if (status.kdf) html.push('<div class="settings-card-text settings-card-note">' + esc(tr('vault.card_kdf', { mib: status.kdf.memlimit_mib, ms: status.kdf.measured_ms },
                'Key derivation: {mib} MiB, about {ms} ms on this device.')) + '</div>');
            if (status.key_locked_in_ram === false) html.push('<div class="settings-card-text settings-card-note vault-note is-warn">' + esc(tr('vault.note_mlock', null, 'The key could not be pinned in memory on this system, so it may be written to swap.')) + '</div>');
        }
        if (status.swap && status.swap.disk_swap) html.push('<div class="settings-card-text settings-card-note vault-note is-warn">' + esc(tr('vault.note_swap', null, 'This device swaps memory to disk. Decrypted data can end up in the swap file. Switch to zram swap or turn swap off.')) + '</div>');
        var buttons = [];
        if (state === 'disabled') {
            buttons.push('<button type="button" class="btn btn-sm" data-vault-action="enable">' + esc(tr('vault.enable', null, 'Enable the vault...')) + '</button>');
        } else if (state === 'locked') {
            buttons.push('<button type="button" class="btn btn-sm" data-vault-action="unlock">' + esc(tr('vault.unlock', null, 'Unlock')) + '</button>');
        } else if (state === 'unlocked') {
            buttons.push('<button type="button" class="btn btn-sm" data-vault-action="lock">' + esc(tr('vault.lock_now', null, 'Lock now')) + '</button>');
            buttons.push('<button type="button" class="btn btn-sm" data-vault-action="change-passphrase">' + esc(tr('vault.change_passphrase', null, 'Change passphrase...')) + '</button>');
            buttons.push('<button type="button" class="btn btn-sm" data-vault-action="new-recovery-key">' + esc(tr('vault.new_recovery_key', null, 'New recovery key...')) + '</button>');
            buttons.push('<button type="button" class="btn btn-sm btn-danger" data-vault-action="disable">' + esc(tr('vault.disable', null, 'Disable the vault...')) + '</button>');
        } else if (state === 'migrating' && !status.progress) {
            buttons.push('<button type="button" class="btn btn-sm" data-vault-action="resume">' + esc(tr('vault.resume', null, 'Finish the job')) + '</button>');
        }
        if (status.recovery_key_pending) buttons.push('<button type="button" class="btn btn-sm" data-vault-action="show-recovery">' + esc(tr('vault.show_recovery', null, 'Show the recovery key')) + '</button>');
        if (buttons.length) html.push('<div class="vault-card-actions">' + buttons.join('') + '</div>');
        (Array.isArray(status.plain_backups) ? status.plain_backups : []).forEach(function (b) { html.push(backupBannerHtml(b)); });
        body.innerHTML = html.join('');
    }

    // The notice inside the Send dialog and any other place that needs a one-line "locked" hint.
    function renderNotices() {
        var hosts = doc.querySelectorAll ? doc.querySelectorAll('[data-vault-notice]') : [];
        Array.prototype.forEach.call(hosts, function (host) {
            if (!isLocked()) { host.hidden = true; host.innerHTML = ''; return; }
            host.hidden = false;
            host.innerHTML = '<div class="vault-banner is-warn" role="status"><span class="vault-banner-text">🔒 ' + esc(tr('vault.send_locked', null, 'MCAttach is locked. Unlock it to send files.')) +
                '</span><button type="button" class="vault-banner-btn" data-vault-action="unlock">' + esc(tr('vault.unlock', null, 'Unlock')) + '</button></div>';
        });
    }

    function renderAll() { renderChip(); renderLines(); renderBanner(); renderCard(); renderNotices(); }

    // ---- the dialog / bottom sheet -------------------------------------------------------------------------------
    function closeSheet() {
        if (!sheet) return;
        var node = sheet.overlay;
        var back = sheet.returnFocus;
        sheet = null;
        busy = false;
        if (node && node.parentNode) node.parentNode.removeChild(node);
        if (back && typeof back.focus === 'function') { try { back.focus(); } catch (e) { /* gone */ } }
    }

    function openSheet(title, options) {
        options = options || {};
        closeSheet();
        var overlay = doc.createElement('div');
        overlay.className = 'mc-vault-overlay';
        var uid = 'mcVaultTitle' + Date.now().toString(36);
        overlay.innerHTML = '<div class="mc-vault-sheet' + (options.danger ? ' is-danger' : '') + '" role="dialog" aria-modal="true" aria-labelledby="' + uid + '">' +
            '<div class="mc-vault-sheet-grip" aria-hidden="true"></div>' +
            '<div class="mc-vault-sheet-head"><h3 id="' + uid + '"></h3><button type="button" class="mc-vault-x" data-vault-close aria-label="' + esc(tr('common.close', null, 'Close')) + '">×</button></div>' +
            '<div class="mc-vault-sheet-body"></div></div>';
        overlay.querySelector('h3').textContent = title;
        doc.body.appendChild(overlay);
        sheet = { overlay: overlay, body: overlay.querySelector('.mc-vault-sheet-body'), box: overlay.querySelector('.mc-vault-sheet'), returnFocus: doc.activeElement };
        overlay.addEventListener('mousedown', function (event) { if (event.target === overlay && !busy) closeSheet(); });
        return sheet;
    }

    function setBody(html, focusSelector) {
        if (!sheet) return null;
        sheet.body.innerHTML = html;
        var target = focusSelector ? sheet.body.querySelector(focusSelector) : sheet.body.querySelector('input, button');
        if (target && target.focus) target.focus();
        return sheet.body;
    }

    function setError(message) {
        if (!sheet) return;
        var line = sheet.body.querySelector('[data-vault-error]');
        if (line) { line.textContent = message || ''; line.hidden = !message; }
    }

    function field(id, label, extra) {
        extra = extra || {};
        return '<label class="mc-vault-field" for="' + id + '"><span>' + esc(label) + '</span><input id="' + id + '" type="' + (extra.type || 'password') + '" ' +
            'autocomplete="' + (extra.autocomplete || 'off') + '" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="done"></label>';
    }

    function strengthMarkup() {
        return '<div class="mc-vault-strength" data-vault-strength aria-live="polite"></div>';
    }

    function bindStrength(input, confirmInput, submitButton, extraOk) {
        var meter = sheet.body.querySelector('[data-vault-strength]');
        function update() {
            var verdict = assessPassphrase(input.value);
            if (meter) {
                meter.className = 'mc-vault-strength is-' + (input.value ? (verdict.ok ? verdict.level : 'bad') : 'empty');
                meter.textContent = input.value ? tr('vault.strength.' + verdict.level, { count: MIN_LENGTH, length: verdict.length }, verdict.level)
                    : tr('vault.strength_hint', { count: MIN_LENGTH }, 'At least {count} characters. A few words in a row are easier to remember than symbols.');
            }
            var matches = !confirmInput || confirmInput.value === input.value;
            var mismatch = sheet.body.querySelector('[data-vault-mismatch]');
            if (mismatch) mismatch.hidden = matches || !confirmInput.value;
            submitButton.disabled = !(verdict.ok && matches && (!extraOk || extraOk()));
        }
        input.addEventListener('input', update);
        if (confirmInput) confirmInput.addEventListener('input', update);
        update();
        return update;
    }

    function closeButton() {
        return '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('common.close', null, 'Close')) + '</button></div>';
    }

    function failedBody(result) {
        setBody('<div class="mc-vault-error" data-vault-error role="alert"></div>' + closeButton());
        setError(errorText(result));
    }

    // ---- unlock --------------------------------------------------------------------------------------------------
    function openUnlock() {
        var box = openSheet(tr('vault.unlock_title', null, 'Unlock MCAttach'));
        var mode = 'passphrase';
        var countdown = null;

        function render() {
            var recovery = mode === 'recovery';
            setBody(
                '<p class="mc-vault-text">' + esc(recovery ? tr('vault.unlock_recovery_text', null, 'Enter the recovery key you saved when you enabled the vault.')
                    : tr('vault.unlock_text', null, 'Enter the vault passphrase. It never leaves this device.')) + '</p>' +
                (recovery ? field('mcVaultSecret', tr('vault.recovery_key', null, 'Recovery key'), { type: 'text', autocomplete: 'off' })
                    : field('mcVaultSecret', tr('vault.passphrase', null, 'Passphrase'), { autocomplete: 'current-password' })) +
                '<div class="mc-vault-error" data-vault-error role="alert" hidden></div>' +
                '<div class="mc-vault-actions"><button type="button" class="mc-vault-link" data-vault-toggle>' + esc(recovery ? tr('vault.use_passphrase', null, 'Use the passphrase') : tr('vault.use_recovery', null, 'Use the recovery key')) + '</button>' +
                '<span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('common.cancel', null, 'Cancel')) + '</button>' +
                '<button type="button" class="btn btn-sm btn-primary" data-vault-submit>' + esc(tr('vault.unlock', null, 'Unlock')) + '</button></div>',
                '#mcVaultSecret');
        }

        function startCountdown(seconds) {
            var submitButton = box.body.querySelector('[data-vault-submit]');
            var left = Math.max(1, Number(seconds) || 1);
            if (countdown) root.clearInterval(countdown);
            function tick() {
                if (!sheet) { root.clearInterval(countdown); return; }
                if (left <= 0) {
                    root.clearInterval(countdown);
                    setError('');
                    if (submitButton) submitButton.disabled = false;
                    return;
                }
                setError(tr('vault.error.unlock_rate_limited', { seconds: left }, 'Too many attempts. Try again in {seconds} s.'));
                if (submitButton) submitButton.disabled = true;
                left--;
            }
            tick();
            countdown = root.setInterval(tick, 1000);
        }

        function submit() {
            var input = box.body.querySelector('#mcVaultSecret');
            if (!input || !input.value || busy) return;
            var payload = mode === 'recovery' ? { recovery_key: input.value } : { passphrase: input.value };
            input.value = '';
            busy = true;
            var button = box.body.querySelector('[data-vault-submit]');
            if (button) button.disabled = true;
            setError('');
            call('POST', '/api/mca/vault/unlock', payload).then(function (result) {
                payload = null;
                busy = false;
                if (!sheet) return;
                if (result.status === 200 && result.data.ok) {
                    if (countdown) root.clearInterval(countdown);
                    closeSheet();
                    setStatus(result.data);
                    refresh();
                    toast(tr('vault.unlocked_toast', null, 'MCAttach is unlocked'), 'success');
                    return;
                }
                if (button) button.disabled = false;
                if (result.data.error_code === 'unlock_rate_limited') { startCountdown(result.data.retry_in_s); return; }
                setError(errorText(result));
                var again = box.body.querySelector('#mcVaultSecret');
                if (again) again.focus();
            });
        }

        render();
        box.body.addEventListener('click', function (event) {
            var target = event.target;
            if (target.closest && target.closest('[data-vault-toggle]')) { mode = mode === 'recovery' ? 'passphrase' : 'recovery'; render(); }
            else if (target.closest && target.closest('[data-vault-submit]')) submit();
        });
        box.body.addEventListener('keydown', function (event) { if (event.key === 'Enter' && event.target && event.target.id === 'mcVaultSecret') { event.preventDefault(); submit(); } });
    }

    // ---- a job's progress (enable, disable, resume) -------------------------------------------------------------
    function watchJob(commandId, onDone, onFail) {
        busy = true;
        function tick() {
            if (!sheet) return;
            Promise.all([call('GET', '/api/mca/vault'), call('GET', '/api/mca/commands/' + encodeURIComponent(commandId))]).then(function (answers) {
                if (!sheet) return;
                var st = answers[0], cmd = answers[1];
                if (st.status === 200 && st.data.ok) {
                    setStatus(st.data);
                    var p = st.data.progress;
                    var line = sheet.body.querySelector('[data-vault-progress]');
                    if (line && p) line.textContent = tr('vault.phase.' + p.phase, null, p.phase) + ' ' + p.done + '/' + p.total;
                    var bar = sheet.body.querySelector('[data-vault-bar]');
                    if (bar && p && p.total) bar.style.width = Math.min(100, Math.round(100 * p.done / p.total)) + '%';
                }
                var command = cmd.data && cmd.data.command ? cmd.data.command : null;
                if (command && command.status === 'succeeded') { busy = false; onDone(command); return; }
                if (command && (command.status === 'failed' || command.status === 'cancelled')) { busy = false; onFail({ status: 409, data: { error_code: command.error_code || '' } }); return; }
                root.setTimeout(tick, 1000);
            });
        }
        tick();
    }

    function progressMarkup(text) {
        return '<p class="mc-vault-text">' + esc(text) + '</p><div class="mc-vault-bar"><div class="mc-vault-bar-fill" data-vault-bar></div></div>' +
            '<div class="mc-vault-text" data-vault-progress role="status"></div><div class="mc-vault-error" data-vault-error role="alert" hidden></div>';
    }

    // ---- the recovery key: shown once, two groups typed back -----------------------------------------------------
    function showRecoveryKey(text, next) {
        var groups = recoveryGroups(text);
        var positions = pickTypeBack(Math.max(groups.length, 2));
        setBody(
            '<p class="mc-vault-text"><strong>' + esc(tr('vault.recovery_title', null, 'Save your recovery key')) + '</strong></p>' +
            '<p class="mc-vault-text">' + esc(tr('vault.recovery_text', null,
                'If you forget the passphrase, this key is the only way back in. It is shown once. If you lose both, the data is gone for good. Write it down and keep it away from this device.')) + '</p>' +
            '<div class="mc-vault-key" data-vault-key>' + groups.map(function (g) { return '<span>' + esc(g) + '</span>'; }).join('') + '</div>' +
            '<div class="mc-vault-actions"><button type="button" class="btn btn-sm" data-vault-copy>' + esc(tr('vault.recovery_copy', null, 'Copy')) + '</button></div>' +
            '<p class="mc-vault-text">' + esc(tr('vault.typeback_text', { a: positions[0], b: positions[1] }, 'To confirm you saved it, type group {a} and group {b}.')) + '</p>' +
            field('mcVaultTb1', tr('vault.typeback_group', { n: positions[0] }, 'Group {n}'), { type: 'text', autocomplete: 'off' }) +
            field('mcVaultTb2', tr('vault.typeback_group', { n: positions[1] }, 'Group {n}'), { type: 'text', autocomplete: 'off' }) +
            '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm btn-primary" data-vault-continue disabled>' + esc(tr('vault.continue', null, 'Continue')) + '</button></div>',
            '#mcVaultTb1');
        var one = sheet.body.querySelector('#mcVaultTb1'), two = sheet.body.querySelector('#mcVaultTb2');
        var go = sheet.body.querySelector('[data-vault-continue]');
        function check() { go.disabled = !typeBackMatches(groups, positions, [one.value, two.value]); }
        one.addEventListener('input', check);
        two.addEventListener('input', check);
        sheet.body.querySelector('[data-vault-copy]').addEventListener('click', function () {
            if (root.navigator && root.navigator.clipboard) root.navigator.clipboard.writeText(text).then(function () { toast(tr('vault.recovery_copied', null, 'Copied'), 'success'); });
        });
        go.addEventListener('click', function () { text = null; groups = null; next(); });
    }

    // ---- the unlock test: lock, then unlock with the passphrase just chosen --------------------------------------
    function unlockTest(done) {
        busy = true;
        call('POST', '/api/mca/vault/lock').then(function (locked) {
            busy = false;
            if (!sheet) return;
            if (!(locked.status === 200 && locked.data.ok)) { failedBody(locked); return; }
            setBody('<p class="mc-vault-text"><strong>' + esc(tr('vault.test_title', null, 'Test your passphrase')) + '</strong></p><p class="mc-vault-text">' + esc(tr('vault.test_text', null,
                'The vault is locked now. Unlock it with the passphrase you just chose to prove you can get back in.')) + '</p>' +
                field('mcVaultSecret', tr('vault.passphrase', null, 'Passphrase'), { autocomplete: 'current-password' }) +
                '<div class="mc-vault-error" data-vault-error role="alert" hidden></div>' +
                '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm btn-primary" data-vault-test>' + esc(tr('vault.unlock', null, 'Unlock')) + '</button></div>', '#mcVaultSecret');
            var input = sheet.body.querySelector('#mcVaultSecret');
            function attempt() {
                if (!input.value || busy) return;
                var secret = input.value;
                input.value = '';
                busy = true;
                setError('');
                call('POST', '/api/mca/vault/unlock', { passphrase: secret }).then(function (result) {
                    secret = null;
                    busy = false;
                    if (!sheet) return;
                    if (result.status === 200 && result.data.ok) { setStatus(result.data); refresh().then(done); return; }
                    setError(errorText(result));
                });
            }
            sheet.body.querySelector('[data-vault-test]').addEventListener('click', attempt);
            input.addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); attempt(); } });
        });
    }

    function finishStep(titleKey, fallback) {
        var backups = status && Array.isArray(status.plain_backups) ? status.plain_backups : [];
        var html = '<p class="mc-vault-text"><strong>' + esc(tr(titleKey, null, fallback)) + '</strong></p>';
        if (backups.length) {
            html += '<p class="mc-vault-text">' + esc(backupLine(backups[0])) + '</p>' +
                '<div class="mc-vault-actions"><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('vault.backup_keep', null, 'Keep it for now')) + '</button><span class="mc-vault-spacer"></span>' +
                '<button type="button" class="btn btn-sm btn-danger" data-vault-action="delete-backup" data-backup="' + esc(backups[0].name) + '" data-bytes="' + esc(backups[0].bytes) + '">' + esc(tr('vault.backup_delete_now', null, 'Delete the plain backup now')) + '</button></div>';
        } else {
            html += closeButton();
        }
        setBody(html, 'button');
    }

    // ---- enable wizard -------------------------------------------------------------------------------------------
    function openEnable() {
        var box = openSheet(tr('vault.enable_title', null, 'Enable the vault'), { danger: true });
        var diskSwap = Boolean(status && status.swap && status.swap.disk_swap);
        setBody(
            '<p class="mc-vault-text">' + esc(tr('vault.enable_intro', null, 'MCAttach will encrypt every file, name, comment and key it stores on this device. You can switch it off again here.')) + '</p>' +
            '<ul class="mc-vault-list">' +
            '<li>' + esc(tr('vault.notice_flash', null, 'Deleting the old plain files does not erase them from SD cards and eMMC storage. Traces of data that existed before may remain. For the strongest protection, enable the vault on a fresh installation.')) + '</li>' +
            '<li>' + esc(tr('vault.notice_gallery', null, 'The Media gallery is not covered. Only MCAttach data is.')) + '</li>' +
            '<li>' + esc(tr('vault.notice_downgrade', null, 'Older versions can not read the vault. Disable it before you go back to an older version; Roll back is refused while it is on.')) + '</li>' +
            '<li>' + esc(tr('vault.notice_forget', null, 'If you forget the passphrase and lose the recovery key, the data is gone for good.')) + '</li>' +
            (diskSwap ? '<li class="is-warn">' + esc(tr('vault.note_swap', null, 'This device swaps memory to disk. Decrypted data can end up in the swap file. Switch to zram swap or turn swap off.')) + '</li>' : '') +
            '</ul>' +
            field('mcVaultNew', tr('vault.passphrase_new', null, 'New passphrase'), { autocomplete: 'new-password' }) + strengthMarkup() +
            field('mcVaultNew2', tr('vault.passphrase_repeat', null, 'Repeat the passphrase'), { autocomplete: 'new-password' }) +
            '<div class="mc-vault-error" data-vault-mismatch hidden>' + esc(tr('vault.mismatch', null, 'The two passphrases are different.')) + '</div>' +
            '<label class="mc-vault-check"><input type="checkbox" id="mcVaultBackup" checked> <span>' + esc(tr('vault.backup_option', null, 'Keep a plain backup of the current data first (recommended). It stays readable until you delete it.')) + '</span></label>' +
            '<label class="mc-vault-check"><input type="checkbox" id="mcVaultAck"> <span>' + esc(tr('vault.ack_flash', null, 'I understand the notice about flash storage.')) + '</span></label>' +
            '<div class="mc-vault-error" data-vault-error role="alert" hidden></div>' +
            '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('common.cancel', null, 'Cancel')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-danger" data-vault-go disabled>' + esc(tr('vault.enable_go', null, 'Enable the vault')) + '</button></div>',
            '#mcVaultNew');
        var input = box.body.querySelector('#mcVaultNew'), again = box.body.querySelector('#mcVaultNew2'), ack = box.body.querySelector('#mcVaultAck');
        var go = box.body.querySelector('[data-vault-go]');
        var update = bindStrength(input, again, go, function () { return ack.checked; });
        ack.addEventListener('change', update);
        go.addEventListener('click', function () {
            if (go.disabled || busy) return;
            var payload = { passphrase: input.value, acknowledge_flash: true, backup: box.body.querySelector('#mcVaultBackup').checked };
            input.value = '';
            again.value = '';
            busy = true;
            go.disabled = true;
            setError('');
            call('POST', '/api/mca/vault/enable', payload).then(function (result) {
                payload = null;
                if (!sheet) return;
                if (!(result.status === 202 && result.data.ok)) {
                    busy = false;
                    setError(errorText(result) + ' ' + tr('vault.enter_again', null, 'Enter the passphrase again.'));
                    update();
                    return;
                }
                setBody(progressMarkup(tr('vault.enabling', null, 'Encrypting your data. Do not close MeshCenter or switch the device off.')));
                watchJob(result.data.command_id, function () {
                    var after = function () { unlockTest(function () { finishStep('vault.enable_done', 'The vault is on.'); }); };
                    call('GET', '/api/mca/vault/recovery-key').then(function (key) {
                        if (!sheet) return;
                        if (key.status === 200 && key.data.recovery_key) showRecoveryKey(key.data.recovery_key, after); else after();
                    });
                }, function (failure) { failedBody(failure); refresh(); });
            });
        });
    }

    // ---- disable -------------------------------------------------------------------------------------------------
    function openDisable() {
        var box = openSheet(tr('vault.disable_title', null, 'Disable the vault'), { danger: true });
        setBody(
            '<p class="mc-vault-text">' + esc(tr('vault.disable_text', null, 'Your files, names, comments and keys are decrypted and stored as plain files again. Anyone with access to this device can then read them.')) + '</p>' +
            '<label class="mc-vault-check"><input type="checkbox" id="mcVaultBackup" checked> <span>' + esc(tr('vault.backup_option_off', null, 'Keep a backup of the encrypted data first (recommended).')) + '</span></label>' +
            '<label class="mc-vault-check"><input type="checkbox" id="mcVaultAck"> <span>' + esc(tr('vault.ack_flash', null, 'I understand the notice about flash storage.')) + '</span></label>' +
            '<div class="mc-vault-error" data-vault-error role="alert" hidden></div>' +
            '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('common.cancel', null, 'Cancel')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-danger" data-vault-go disabled>' + esc(tr('vault.disable_go', null, 'Disable the vault')) + '</button></div>',
            'button[data-vault-close]');
        var ack = box.body.querySelector('#mcVaultAck'), go = box.body.querySelector('[data-vault-go]');
        ack.addEventListener('change', function () { go.disabled = !ack.checked; });
        go.addEventListener('click', function () {
            if (go.disabled || busy) return;
            var payload = { acknowledge_flash: true, backup: box.body.querySelector('#mcVaultBackup').checked };
            busy = true;
            go.disabled = true;
            call('POST', '/api/mca/vault/disable', payload).then(function (result) {
                if (!sheet) return;
                if (!(result.status === 202 && result.data.ok)) { busy = false; setError(errorText(result)); return; }
                setBody(progressMarkup(tr('vault.disabling', null, 'Decrypting your data. Do not close MeshCenter or switch the device off.')));
                watchJob(result.data.command_id, function () { refresh().then(function () { finishStep('vault.disable_done', 'The vault is off.'); }); }, function (failure) { failedBody(failure); refresh(); });
            });
        });
    }

    // ---- resume, change passphrase, new recovery key -------------------------------------------------------------
    function resumeJob() {
        call('POST', '/api/mca/vault/resume').then(function (result) {
            if (!(result.status === 202 && result.data.ok)) { toast(errorText(result), 'error', { duration: 10000 }); return; }
            openSheet(tr('vault.resume_title', null, 'Finishing the vault job'));
            setBody(progressMarkup(tr('vault.resuming', null, 'Finishing the interrupted job. Do not close MeshCenter.')));
            watchJob(result.data.command_id, function () { refresh().then(function () { finishStep('vault.resume_done', 'The vault job is finished.'); }); }, function (failure) { failedBody(failure); refresh(); });
        });
    }

    function openChangePassphrase() {
        var box = openSheet(tr('vault.change_title', null, 'Change the passphrase'));
        setBody(
            field('mcVaultCur', tr('vault.passphrase_current', null, 'Current passphrase'), { autocomplete: 'current-password' }) +
            field('mcVaultNew', tr('vault.passphrase_new', null, 'New passphrase'), { autocomplete: 'new-password' }) + strengthMarkup() +
            field('mcVaultNew2', tr('vault.passphrase_repeat', null, 'Repeat the passphrase'), { autocomplete: 'new-password' }) +
            '<div class="mc-vault-error" data-vault-mismatch hidden>' + esc(tr('vault.mismatch', null, 'The two passphrases are different.')) + '</div>' +
            '<div class="mc-vault-error" data-vault-error role="alert" hidden></div>' +
            '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('common.cancel', null, 'Cancel')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-primary" data-vault-go disabled>' + esc(tr('vault.change_go', null, 'Change')) + '</button></div>', '#mcVaultCur');
        var cur = box.body.querySelector('#mcVaultCur'), input = box.body.querySelector('#mcVaultNew'), again = box.body.querySelector('#mcVaultNew2'), go = box.body.querySelector('[data-vault-go]');
        var update = bindStrength(input, again, go, function () { return Boolean(cur.value); });
        cur.addEventListener('input', update);
        go.addEventListener('click', function () {
            if (go.disabled || busy) return;
            var payload = { current_passphrase: cur.value, new_passphrase: input.value };
            cur.value = '';
            input.value = '';
            again.value = '';
            busy = true;
            go.disabled = true;
            setError('');
            call('POST', '/api/mca/vault/change-passphrase', payload).then(function (result) {
                payload = null;
                busy = false;
                if (!sheet) return;
                if (result.status === 200 && result.data.ok) { closeSheet(); toast(tr('vault.change_done', null, 'The passphrase was changed. The recovery key still works.'), 'success'); refresh(); return; }
                setError(errorText(result));
                update();
            });
        });
    }

    function openNewRecoveryKey() {
        var box = openSheet(tr('vault.newkey_title', null, 'New recovery key'));
        setBody('<p class="mc-vault-text">' + esc(tr('vault.newkey_text', null, 'The old recovery key stops working. The new one is shown once.')) + '</p>' +
            field('mcVaultCur', tr('vault.passphrase_current', null, 'Current passphrase'), { autocomplete: 'current-password' }) +
            '<div class="mc-vault-error" data-vault-error role="alert" hidden></div>' +
            '<div class="mc-vault-actions"><span class="mc-vault-spacer"></span><button type="button" class="btn btn-sm" data-vault-close>' + esc(tr('common.cancel', null, 'Cancel')) + '</button>' +
            '<button type="button" class="btn btn-sm btn-primary" data-vault-go>' + esc(tr('vault.newkey_go', null, 'Make a new key')) + '</button></div>', '#mcVaultCur');
        var cur = box.body.querySelector('#mcVaultCur'), go = box.body.querySelector('[data-vault-go]');
        function submit() {
            if (!cur.value || busy) return;
            var payload = { current_passphrase: cur.value };
            cur.value = '';
            busy = true;
            go.disabled = true;
            setError('');
            call('POST', '/api/mca/vault/new-recovery-key', payload).then(function (result) {
                payload = null;
                busy = false;
                if (!sheet) return;
                if (result.status === 200 && result.data.recovery_key) { showRecoveryKey(result.data.recovery_key, function () { refresh(); finishStep('vault.newkey_done', 'The new recovery key is active.'); }); return; }
                go.disabled = false;
                setError(errorText(result));
            });
        }
        go.addEventListener('click', submit);
        cur.addEventListener('keydown', function (event) { if (event.key === 'Enter') { event.preventDefault(); submit(); } });
    }

    function showPendingRecoveryKey() {
        openSheet(tr('vault.recovery_title', null, 'Save your recovery key'));
        call('GET', '/api/mca/vault/recovery-key').then(function (key) {
            if (!sheet) return;
            if (key.status === 200 && key.data.recovery_key) showRecoveryKey(key.data.recovery_key, function () { refresh(); finishStep('vault.recovery_saved', 'Recovery key confirmed.'); });
            else failedBody(key);
        });
    }

    // ---- lock now, delete the plain backup -----------------------------------------------------------------------
    function lockNow() {
        call('POST', '/api/mca/vault/lock').then(function (result) {
            if (result.status === 200 && result.data.ok) { setStatus(result.data); refresh(); toast(tr('vault.locked_toast', null, 'MCAttach is locked'), 'success'); }
            else toast(errorText(result), 'error', { duration: 10000 });
        });
    }

    function deleteBackup(name, bytes, encrypted) {
        if (typeof root.appConfirm !== 'function') return;
        root.appConfirm({
            title: encrypted ? tr('vault.backup_delete_title_enc', null, 'Delete the backup?') : tr('vault.backup_delete_title', null, 'Delete the plain backup?'),
            message: encrypted
                ? tr('vault.backup_delete_text_enc', { folder: name, size: size(Number(bytes)) }, 'The folder {folder} ({size}) holds a copy of your encrypted MCAttach data. It is deleted for good.')
                : tr('vault.backup_delete_text', { folder: name, size: size(Number(bytes)) },
                    'The folder {folder} ({size}) holds a readable copy of your MCAttach data. It is deleted for good. On SD cards and eMMC, traces can remain until the blocks are reused.'),
            confirmLabel: encrypted ? tr('vault.backup_delete_enc', null, 'Delete the backup') : tr('vault.backup_delete', null, 'Delete the plain backup'), cancelLabel: tr('common.cancel', null, 'Cancel'), danger: true
        }).then(function (ok) {
            if (!ok) return;
            call('POST', '/api/mca/vault/backup/delete', { name: name }).then(function (result) {
                if (result.status === 200 && result.data.ok) { toast(tr('vault.backup_deleted', null, 'The plain backup was deleted'), 'success'); if (sheet) closeSheet(); refresh(); }
                else toast(errorText(result), 'error', { duration: 10000 });
            });
        });
    }

    function lockConfirm() {
        if (typeof root.appConfirm !== 'function') { lockNow(); return; }
        root.appConfirm({ title: tr('vault.lock_title', null, 'Lock MCAttach?'), message: tr('vault.lock_text', null, 'Transfers are hidden and files can not be sent or opened until you unlock it again.'),
            confirmLabel: tr('vault.lock_now', null, 'Lock now'), cancelLabel: tr('common.cancel', null, 'Cancel') }).then(function (ok) { if (ok) lockNow(); });
    }

    // ---- events --------------------------------------------------------------------------------------------------
    doc.addEventListener('click', function (event) {
        var target = event.target;
        if (!target || !target.closest) return;
        var closer = target.closest('[data-vault-close]');
        if (closer && sheet && !busy) { closeSheet(); return; }
        var chip = target.closest('#dockVaultChip');
        if (chip) {
            var st = chip.getAttribute('data-vault-state');
            if (st === 'locked') openUnlock(); else if (st === 'unlocked') lockConfirm(); else refresh();
            return;
        }
        var el = target.closest('[data-vault-action]');
        if (!el) return;
        var action = el.getAttribute('data-vault-action');
        if (action === 'unlock') openUnlock();
        else if (action === 'lock') lockConfirm();
        else if (action === 'enable') openEnable();
        else if (action === 'disable') openDisable();
        else if (action === 'resume') resumeJob();
        else if (action === 'change-passphrase') openChangePassphrase();
        else if (action === 'new-recovery-key') openNewRecoveryKey();
        else if (action === 'show-recovery') showPendingRecoveryKey();
        else if (action === 'delete-backup') deleteBackup(el.getAttribute('data-backup'), el.getAttribute('data-bytes'), el.getAttribute('data-encrypted') === '1');
    });

    doc.addEventListener('keydown', function (event) {
        if (!sheet) return;
        if (event.key === 'Escape' && !busy) { event.preventDefault(); closeSheet(); return; }
        if (event.key === 'Tab') {
            var items = sheet.box.querySelectorAll('input:not([disabled]), button:not([disabled]), a[href]');
            if (!items.length) return;
            var first = items[0], last = items[items.length - 1];
            if (event.shiftKey && doc.activeElement === first) { event.preventDefault(); last.focus(); }
            else if (!event.shiftKey && doc.activeElement === last) { event.preventDefault(); first.focus(); }
        }
    });

    doc.addEventListener('visibilitychange', function () { if (!doc.hidden) refresh(); });

    api.refresh = refresh;
    api.isLocked = isLocked;
    api.isEnabled = isEnabled;
    api.openUnlock = openUnlock;
    api.noteLocked = refresh;               // a 423 vault_locked from anywhere: re-read the state so the chip and banners catch up
    api.status = function () { return status; };
    root.MCVault = api;

    // the catalog loads after this script: draw again once it has arrived (the first draw used the English fallbacks)
    function redrawWhenCatalogLoads(tries) {
        var i18n = root.I18N;
        if (i18n && typeof i18n.tOrFallback === 'function' && i18n.tOrFallback('vault.unlock', null, null) !== null) { renderAll(); return; }
        if (tries > 0) root.setTimeout(function () { redrawWhenCatalogLoads(tries - 1); }, 250);
    }

    function boot() { refresh(); redrawWhenCatalogLoads(40); }
    if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot); else boot();
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : this));
