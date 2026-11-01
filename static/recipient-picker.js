// ============================================================
// RECIPIENT PICKER (UX5)
// One searchable picker for every place that picks an MCAttach recipient: the Send dialog of the Files tab, the Send
// pictures dialog of the Media gallery (G2) and the "Send camera picture" schedule action (G3). It replaces a native
// <select> that listed 600+ nodes, most of them "Key unknown".
//
//  - search: case-insensitive substring of the short name, the long name and the node id (!xxxx), live, with a clear button
//    and a live "N of M" count;
//  - "Favorites only" (the node favorites of the Nodes tab), remembered per kind of device, on by default when the user has
//    at least one favorite that can be sent to;
//  - order: recently used recipients first, then contacts with a known key, then (own group, disabled, never selectable)
//    nodes without a key, with a "Request key" action where the host offers one;
//  - fast with 1000+ nodes: only the filtered rows are rendered, capped at CAP, with a "type to narrow" hint.
//
// The pure logic (matches / build / createModel) has no DOM and is unit-tested in tests/frontend/test_recipient_picker.mjs.
// ============================================================
(function (root) {
    'use strict';

    var CAP = 50;              // rows rendered at once (a phone should never build 600 buttons)
    var RECENT_MAX = 8;        // recipients remembered as "recently used"

    function lower(value) {
        return String(value == null ? '' : value).toLowerCase();
    }

    function normalizeQuery(value) {
        return String(value == null ? '' : value).split(/\s+/).filter(Boolean).join(' ');
    }

    function escapeHtml(value) {
        return String(value == null ? '' : value)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#039;');
    }

    // ---- pure logic ------------------------------------------------------------------------------------------------

    var searchKeys = typeof WeakMap === 'function' ? new WeakMap() : null;

    // "short name, long name, id" in one lower-case string, built once per contact object
    function searchKey(contact) {
        if (searchKeys && searchKeys.has(contact)) return searchKeys.get(contact);
        var key = lower(contact.short_name) + '\n' + lower(contact.name) + '\n' + lower(contact.id);
        if (searchKeys) searchKeys.set(contact, key);
        return key;
    }

    // An empty query matches everything. A node id is also found without its leading "!" (hex digits only).
    function matches(contact, query) {
        var needle = lower(normalizeQuery(query));
        if (!needle) return true;
        var key = searchKey(contact);
        if (key.indexOf(needle) !== -1) return true;
        return /^[0-9a-f]+$/.test(needle) && key.indexOf('!' + needle) !== -1;
    }

    function byName(a, b) {
        var an = lower(a.name || a.short_name || a.id);
        var bn = lower(b.name || b.short_name || b.id);
        return an < bn ? -1 : an > bn ? 1 : (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
    }

    // What to show for a query. `found` is the N of "N of M" (everything that matches), `total` the M; only `rows` are
    // rendered (the first `cap` of them), `truncated` says there were more.
    //   groups, in this order: recent (only while no query is typed), ready (a key is known), nokey (disabled).
    function build(contacts, options) {
        var o = options || {};
        var list = Array.isArray(contacts) ? contacts : [];
        var cap = o.cap > 0 ? o.cap : CAP;
        var recentIds = Array.isArray(o.recent) ? o.recent : [];
        var query = normalizeQuery(o.query);
        var filtered = list.filter(function (c) { return matches(c, query) && (!o.favoritesOnly || c.favorite === true); });
        var ready = filtered.filter(function (c) { return c.can_send === true; });
        var nokey = filtered.filter(function (c) { return c.can_send !== true; }).sort(byName);

        var recent = [];
        if (!query) {
            var readyById = {};
            ready.forEach(function (c) { readyById[c.id] = c; });
            recentIds.forEach(function (id) {
                if (readyById[id] && recent.indexOf(readyById[id]) === -1) recent.push(readyById[id]);
            });
        }
        var rest = ready.filter(function (c) { return recent.indexOf(c) === -1; }).sort(byName);

        var rows = [];
        var used = 0;
        function take(group, items) {
            var taken = items.slice(0, Math.max(0, cap - used));
            if (!taken.length) return;
            rows.push({ kind: 'header', group: group, count: items.length });
            taken.forEach(function (c) { rows.push({ kind: 'row', group: group, contact: c }); });
            used += taken.length;
        }
        take('recent', recent);
        take('ready', rest);
        take('nokey', nokey);
        // a header is only worth showing when there is more than one group on screen (the no-key group always says so)
        var headers = rows.filter(function (r) { return r.kind === 'header'; });
        if (headers.length === 1 && headers[0].group !== 'nokey') rows.shift();
        return {
            rows: rows,
            found: filtered.length,
            total: list.length,
            shown: used,
            truncated: filtered.length > used,
            readyFound: ready.length,
            query: query,
        };
    }

    // The state behind one picker: the contacts, the query, "Favorites only", the recents and the chosen recipient.
    // Only a contact that can be sent to can be chosen; a chosen one that disappears or loses its key is dropped.
    function createModel(initial) {
        var s = initial || {};
        var model = {
            contacts: [],
            query: '',
            favoritesOnly: false,
            favoritesTouched: s.favoritesOnly === true || s.favoritesOnly === false,
            recent: Array.isArray(s.recent) ? s.recent.slice(0, RECENT_MAX) : [],
            selected: '',
            cap: s.cap > 0 ? s.cap : CAP,
        };
        if (model.favoritesTouched) model.favoritesOnly = s.favoritesOnly;

        function find(id) {
            for (var i = 0; i < model.contacts.length; i++) if (model.contacts[i].id === id) return model.contacts[i];
            return null;
        }

        // "on by default if the user has at least one favorite with a known key"
        function defaultFavoritesOnly(contacts) {
            return contacts.some(function (c) { return c.favorite === true && c.can_send === true; });
        }

        model.setContacts = function (contacts) {
            model.contacts = Array.isArray(contacts) ? contacts : [];
            if (!model.favoritesTouched) model.favoritesOnly = defaultFavoritesOnly(model.contacts);
            var chosen = find(model.selected);
            if (model.selected && !(chosen && chosen.can_send === true)) model.selected = '';
        };
        model.setQuery = function (query) { model.query = String(query == null ? '' : query); };
        model.setFavoritesOnly = function (value) { model.favoritesOnly = Boolean(value); model.favoritesTouched = true; };
        model.setRecent = function (ids) { model.recent = Array.isArray(ids) ? ids.slice(0, RECENT_MAX) : []; };
        model.select = function (id) {
            var c = find(id);
            if (!c || c.can_send !== true) return false;      // a node without a key is never a target
            model.selected = id;
            return true;
        };
        model.clearSelection = function () { model.selected = ''; };
        model.selectedContact = function () { return model.selected ? find(model.selected) : null; };
        model.view = function () {
            return build(model.contacts, { query: model.query, favoritesOnly: model.favoritesOnly, recent: model.recent, cap: model.cap });
        };
        model.defaultFavoritesOnly = function () { return defaultFavoritesOnly(model.contacts); };
        return model;
    }

    // the new "recently used" list after a send to `id`: it moves to the front, no duplicates, at most RECENT_MAX
    function recentAfterUse(recent, id) {
        var list = Array.isArray(recent) ? recent.filter(function (x) { return x !== id; }) : [];
        if (id) list.unshift(id);
        return list.slice(0, RECENT_MAX);
    }

    // ---- preferences (settings.send_recipients), per kind of device -------------------------------------------------

    function isPhone() {
        return Boolean(root.MCMobileLayout && typeof root.MCMobileLayout.isMobile === 'function' && root.MCMobileLayout.isMobile());
    }

    function favoritesField(phone) {
        return phone ? 'favorites_only_mobile' : 'favorites_only_desktop';
    }

    function readPrefs(settings) {
        var s = settings && settings.send_recipients && typeof settings.send_recipients === 'object' ? settings.send_recipients : {};
        var fav = s[favoritesField(isPhone())];
        return {
            favoritesOnly: fav === true || fav === false ? fav : null,
            recent: Array.isArray(s.recent) ? s.recent.filter(function (x) { return typeof x === 'string'; }).slice(0, RECENT_MAX) : [],
        };
    }

    var prefsCache = null;       // the last settings.send_recipients this page knows

    function currentSettings() {
        return root.appSettings && typeof root.appSettings === 'object' ? root.appSettings : null;
    }

    function loadPrefs() {
        var known = currentSettings();
        if (known && known.send_recipients) { prefsCache = readPrefs(known); return Promise.resolve(prefsCache); }
        if (prefsCache) return Promise.resolve(prefsCache);
        if (typeof fetch !== 'function') return Promise.resolve(readPrefs(null));
        return fetch('/api/settings').then(function (r) { return r.json(); }).then(function (data) {
            prefsCache = readPrefs(data && data.settings);
            return prefsCache;
        }, function () { return readPrefs(null); });
    }

    function savePrefs(patch) {
        if (typeof fetch !== 'function') return Promise.resolve();
        return fetch('/api/settings', {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ send_recipients: patch }),
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (data && data.ok && data.settings && currentSettings()) root.appSettings = data.settings;
        }, function () { /* just not remembered */ });
    }

    // Called by a send (or a saved schedule rule) that went to `id`: it becomes the first of the recently used.
    function noteUsed(id) {
        if (!id) return Promise.resolve();
        return loadPrefs().then(function (prefs) {
            prefsCache = { favoritesOnly: prefs.favoritesOnly, recent: recentAfterUse(prefs.recent, id) };
            return savePrefs({ recent: prefsCache.recent });
        });
    }

    // ---- HTML ------------------------------------------------------------------------------------------------------

    function icon(path) {
        return '<svg class="rp-ico-svg" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" '
            + 'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' + path + '</svg>';
    }
    var ICON_SEARCH = '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>';
    var ICON_CLOSE = '<path d="M6 6l12 12"/><path d="M18 6L6 18"/>';

    // Everything the host supplies: translations (`t(key, params, fallback)`), the label of a contact without a key, and the
    // attribute (if any) that turns a "Request key" button into the host's own action.
    function renderRowHtml(row, ctx, selectedId) {
        var c = row.contact;
        var title = c.name || c.short_name || c.id;
        var sub = [c.short_name && c.short_name !== title ? c.short_name : '', c.id].filter(Boolean).join(' · ');
        var star = c.favorite ? '<span class="rp-star" aria-hidden="true">★</span>' : '';
        var main = star + '<span class="rp-main"><strong class="rp-name">' + escapeHtml(title) + '</strong>'
            + '<span class="rp-sub">' + escapeHtml(sub) + '</span></span>';
        if (c.can_send === true) {
            var on = c.id === selectedId;
            return '<button type="button" class="rp-row' + (on ? ' is-selected' : '') + '" role="option" aria-selected="' + on
                + '" data-rp-id="' + escapeHtml(c.id) + '">' + main + (on ? '<span class="rp-check" aria-hidden="true">✓</span>' : '') + '</button>';
        }
        var reason = ctx.reasonLabel ? ctx.reasonLabel(c) : ctx.t('recipient.key_unknown', null, 'Key unknown, request the key');
        var action = ctx.requestKeyAttr && c.can_request_key
            ? '<button type="button" class="rp-request" ' + ctx.requestKeyAttr + ' data-contact="' + escapeHtml(c.id) + '">'
                + escapeHtml(ctx.t('recipient.request_key', null, 'Request key')) + '</button>' : '';
        return '<div class="rp-row is-disabled" role="option" aria-disabled="true" aria-selected="false" data-rp-nokey="' + escapeHtml(c.id) + '">'
            + main + '<span class="rp-reason">' + escapeHtml(reason) + '</span>' + action + '</div>';
    }

    function renderListHtml(view, ctx, selectedId) {
        if (!view.rows.length) {
            return '<div class="rp-empty" role="status">' + escapeHtml(view.total
                ? ctx.t('recipient.no_match', null, 'No contact matches') : ctx.t('recipient.none', null, 'No known contacts yet.')) + '</div>';
        }
        var html = view.rows.map(function (row) {
            if (row.kind === 'header') {
                var key = row.group === 'recent' ? 'recipient.group_recent' : row.group === 'ready' ? 'recipient.group_ready' : 'recipient.group_nokey';
                var fallback = row.group === 'recent' ? 'Recently used' : row.group === 'ready' ? 'Contacts you can send to' : 'No key yet';
                return '<div class="rp-group" role="presentation">' + escapeHtml(ctx.t(key, null, fallback)) + '</div>';
            }
            return renderRowHtml(row, ctx, selectedId);
        }).join('');
        if (view.truncated) {
            html += '<div class="rp-more" role="status">' + escapeHtml(ctx.t('recipient.type_to_narrow', { shown: view.shown, found: view.found },
                'Showing ' + view.shown + ' of ' + view.found + ', type to narrow')) + '</div>';
        }
        return html;
    }

    function frameHtml(ctx, id) {
        var t = ctx.t;
        var label = escapeHtml(t('recipient.search_label', null, 'Search contacts'));
        return '<div class="rp-top">'
            + '<div class="rp-field"><span class="rp-ico" aria-hidden="true">' + icon(ICON_SEARCH) + '</span>'
            + '<input type="text" class="rp-input" id="' + escapeHtml(id) + 'Search" autocomplete="off" autocapitalize="off" spellcheck="false" enterkeyhint="search" maxlength="60" '
            + 'placeholder="' + escapeHtml(t('recipient.search_placeholder', null, 'Name or ID')) + '" aria-label="' + label + '">'
            + '<button type="button" class="rp-clear" data-rp-clear aria-label="' + escapeHtml(t('recipient.clear', null, 'Clear search'))
            + '" title="' + escapeHtml(t('recipient.clear', null, 'Clear search')) + '" hidden>' + icon(ICON_CLOSE) + '</button></div>'
            + '<span class="rp-count" role="status" aria-live="polite"></span></div>'
            + '<label class="rp-favs"><input type="checkbox" class="rp-favs-box" data-rp-favs> <span>'
            + escapeHtml(t('recipient.favorites_only', null, 'Favorites only')) + '</span></label>'
            + '<div class="rp-list" role="listbox" aria-label="' + escapeHtml(t('recipient.list_label', null, 'Contacts')) + '"></div>'
            + '<div class="rp-selected" aria-live="polite"></div>';
    }

    // ---- DOM glue --------------------------------------------------------------------------------------------------
    //
    // mount(host, { id, t, onChange, reasonLabel, requestKeyAttr, selected }) draws the picker into `host` and returns
    // { setContacts, setSelected, getSelected, refresh, model }. The list is the only part re-rendered while typing, so the
    // field keeps the focus.
    function mount(host, options) {
        var o = options || {};
        var ctx = {
            t: o.t || function (key, params, fallback) { return fallback || key; },
            reasonLabel: o.reasonLabel, requestKeyAttr: o.requestKeyAttr,
        };
        var id = o.id || 'rp';
        var model = createModel({ cap: o.cap });
        host.innerHTML = frameHtml(ctx, id);
        var input = host.querySelector('.rp-input');
        var clear = host.querySelector('[data-rp-clear]');
        var favs = host.querySelector('[data-rp-favs]');
        var list = host.querySelector('.rp-list');
        var count = host.querySelector('.rp-count');
        var selectedEl = host.querySelector('.rp-selected');
        var touchedPrefs = false;

        // A host without a real DOM (a test's fake document) gets the same markup, written as one string, and no events.
        if (!input || !clear || !favs || !list || !count || !selectedEl) {
            var headless = {
                model: model,
                setContacts: function (contacts) {
                    model.setContacts(contacts);
                    host.innerHTML = frameHtml(ctx, id) + renderListHtml(model.view(), ctx, model.selected);
                },
                setSelected: function (value) { if (value) model.select(value); else model.clearSelection(); },
                getSelected: function () { return model.selected; },
                refresh: function () {},
            };
            return headless;
        }

        function paint() {
            var view = model.view();
            list.innerHTML = renderListHtml(view, ctx, model.selected);
            var active = Boolean(normalizeQuery(model.query)) || model.favoritesOnly;
            count.textContent = active ? ctx.t('recipient.count', { shown: view.found, total: view.total }, view.found + ' of ' + view.total) : '';
            count.className = 'rp-count' + (active && view.found === 0 ? ' is-empty' : '');
            clear.hidden = !model.query;
            favs.checked = model.favoritesOnly;
            var chosen = model.selectedContact();
            var name = chosen ? (chosen.name || chosen.short_name || chosen.id) : '';
            selectedEl.textContent = chosen ? ctx.t('recipient.selected', { name: name }, 'Selected: ' + name) : '';
        }

        function changed() { if (typeof o.onChange === 'function') o.onChange(model.selected, model.selectedContact()); }

        input.addEventListener('input', function () { model.setQuery(input.value); paint(); });
        host.addEventListener('click', function (event) {
            var target = event.target;
            if (!target || typeof target.closest !== 'function') return;
            if (target.closest('[data-rp-clear]')) { input.value = ''; model.setQuery(''); paint(); input.focus(); return; }
            var row = target.closest('[data-rp-id]');
            if (row && model.select(row.getAttribute('data-rp-id'))) { paint(); changed(); }
        });
        favs.addEventListener('change', function () {
            model.setFavoritesOnly(favs.checked);
            touchedPrefs = true;
            var patch = {};
            patch[favoritesField(isPhone())] = model.favoritesOnly;
            savePrefs(patch);
            paint();
        });

        var controller = {
            setContacts: function (contacts) {
                var before = model.selected;
                model.setContacts(contacts);
                paint();
                if (before !== model.selected) changed();
            },
            setSelected: function (value) { if (value) model.select(value); else model.clearSelection(); paint(); },
            getSelected: function () { return model.selected; },
            refresh: paint,
            model: model,
        };
        // the remembered choices arrive asynchronously; they never override what the user has already touched
        loadPrefs().then(function (prefs) {
            if (!touchedPrefs && (prefs.favoritesOnly === true || prefs.favoritesOnly === false)) {
                model.setFavoritesOnly(prefs.favoritesOnly);
            }
            model.setRecent(prefs.recent);
            paint();
        });
        if (o.selected) model.select(o.selected);
        return controller;
    }

    root.MCRecipientPicker = {
        CAP: CAP, RECENT_MAX: RECENT_MAX,
        matches: matches, build: build, createModel: createModel, recentAfterUse: recentAfterUse,
        readPrefs: readPrefs, favoritesField: favoritesField, loadPrefs: loadPrefs, savePrefs: savePrefs, noteUsed: noteUsed,
        renderListHtml: renderListHtml, renderRowHtml: renderRowHtml, frameHtml: frameHtml, mount: mount, escapeHtml: escapeHtml,
    };
})(typeof window !== 'undefined' ? window : globalThis);
