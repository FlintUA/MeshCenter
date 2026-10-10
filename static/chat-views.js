// ============================================================
// chat-views.js (U1) - pure, DOM-free view logic shared by chat.js:
//   * message/chat timestamp formatting (today / yesterday / this year / older)
//   * "first unread" placement for an opened chat
//   * node-list sort + filter predicates (Nodes sidebar)
//
// Kept separate from chat.js so tests/frontend/test_chat_views.mjs can load
// the REAL code under node's `vm` without a DOM. Nothing here touches the
// document, localStorage or the network; chat.js owns all of that and passes
// what is needed in (a `now`, a translator, per-node helpers).
// ============================================================
(function (root) {
    'use strict';

    // ---------------- Timestamp formatting ----------------

    function pad2(n) { return n < 10 ? '0' + n : String(n); }

    // Day-first locales for every UI locale (the product shows 08.10 18:36 style
    // dates); plain 'en' would otherwise resolve to US month-first.
    function intlLocale(locale) {
        return locale === 'en' || !locale ? 'en-GB' : locale;
    }

    // ts: epoch seconds. Returns '' when ts is not usable so the caller can fall
    // back to the legacy "HH:MM:SS" string stored on old messages.
    //   options.now    - epoch ms (default Date.now())
    //   options.locale - UI locale code
    //   options.t      - I18N.t-style translator; used for the "yesterday" prefix
    function formatMessageTime(ts, options) {
        const opts = options || {};
        const seconds = Number(ts);
        if (ts === null || ts === undefined || ts === '' || !Number.isFinite(seconds) || seconds <= 0) {
            return '';
        }
        const date = new Date(seconds * 1000);
        if (Number.isNaN(date.getTime())) return '';
        const now = new Date(opts.now !== undefined ? opts.now : Date.now());
        const locale = intlLocale(opts.locale);

        const time = pad2(date.getHours()) + ':' + pad2(date.getMinutes());

        // Calendar-day comparison in local time (never "86400 s ago"), so DST
        // changes and the midnight edges behave.
        const dayStart = new Date(date.getFullYear(), date.getMonth(), date.getDate());
        const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const yesterdayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1);

        if (dayStart.getTime() === todayStart.getTime()) return time;
        if (dayStart.getTime() === yesterdayStart.getTime()) {
            const translate = typeof opts.t === 'function' ? opts.t : null;
            const prefix = translate ? translate('chat.date_yesterday') : 'yesterday';
            return prefix + ' ' + time;
        }

        const sameYear = date.getFullYear() === now.getFullYear();
        const dateText = new Intl.DateTimeFormat(locale, sameYear
            ? { day: '2-digit', month: '2-digit' }
            : { day: '2-digit', month: '2-digit', year: 'numeric' }).format(date);
        return dateText + ' ' + time;
    }

    // ---------------- First unread ----------------

    // The chat list's unread count N is the number of messages from the end that
    // are unread. Returns the index of the first unread message among `total`
    // loaded ones, or -1 when there is nothing to mark. N larger than what is
    // loaded clamps to the oldest loaded message.
    function firstUnreadIndex(total, unreadCount) {
        const n = Math.floor(Number(unreadCount));
        const len = Math.floor(Number(total));
        if (!Number.isFinite(n) || !Number.isFinite(len) || n <= 0 || len <= 0) return -1;
        return Math.max(0, len - n);
    }

    // ---------------- Node sort / filters ----------------

    const SORT_KEYS = ['last_heard', 'name', 'distance', 'hops', 'favorites_first'];
    const OFFLINE_AFTER_SECONDS = 2 * 60 * 60;
    // Real Meshtastic Config.DeviceConfig.Role names for relaying infrastructure.
    // (ROUTER_CLIENT is the deprecated pre-2.3 name of the same behaviour.)
    const INFRASTRUCTURE_ROLES = ['ROUTER', 'ROUTER_LATE', 'REPEATER', 'ROUTER_CLIENT'];

    const DEFAULT_NODE_FILTERS = Object.freeze({
        sort: 'last_heard',
        favoritesOnly: false,
        ignoredOnly: false,
        hideOffline: false,
        directOnly: false,
        hideInfrastructure: false,
        knownKeyOnly: false,
    });

    const FILTER_FLAGS = ['favoritesOnly', 'ignoredOnly', 'hideOffline', 'directOnly',
        'hideInfrastructure', 'knownKeyOnly'];

    function normalizeNodeFilters(raw) {
        const out = Object.assign({}, DEFAULT_NODE_FILTERS);
        if (!raw || typeof raw !== 'object') return out;
        if (SORT_KEYS.indexOf(raw.sort) !== -1) out.sort = raw.sort;
        FILTER_FLAGS.forEach(function (flag) { out[flag] = raw[flag] === true; });
        return out;
    }

    function activeFilterCount(state) {
        const s = normalizeNodeFilters(state);
        return FILTER_FLAGS.reduce(function (sum, flag) { return sum + (s[flag] ? 1 : 0); }, 0);
    }

    function nodeHops(node) {
        const raw = node && node.hops_away !== undefined && node.hops_away !== null && node.hops_away !== ''
            ? node.hops_away
            : (node ? node.hop_start : undefined);
        if (raw === undefined || raw === null || raw === '') return null;
        const hops = Number(raw);
        return Number.isFinite(hops) && hops >= 0 ? hops : null;
    }

    function nodeLastSeen(node) {
        const seen = Number(node && node.last_seen);
        return Number.isFinite(seen) && seen > 0 ? seen : 0;
    }

    function isInfrastructureRole(role) {
        return INFRASTRUCTURE_ROLES.indexOf(String(role || '').toUpperCase()) !== -1;
    }

    // context: { now: epoch seconds, hasKnownKey(node) -> bool }
    function nodePassesFilters(node, state, context) {
        const s = normalizeNodeFilters(state);
        const ctx = context || {};
        const ignored = Boolean(node.ignored);

        // DEFAULT: ignored nodes stay out of the normal list; they show only
        // through "ignored only".
        if (s.ignoredOnly ? !ignored : ignored) return false;
        if (s.favoritesOnly && !node.favorite) return false;
        if (s.hideOffline) {
            const seen = nodeLastSeen(node);
            const now = Number(ctx.now);
            if (!seen || (Number.isFinite(now) && now - seen > OFFLINE_AFTER_SECONDS)) return false;
        }
        if (s.directOnly && nodeHops(node) !== 0) return false;
        if (s.hideInfrastructure && isInfrastructureRole(node.role)) return false;
        if (s.knownKeyOnly && !(typeof ctx.hasKnownKey === 'function' && ctx.hasKnownKey(node))) return false;
        return true;
    }

    function compareNames(a, b) {
        return String(a.clean_name || a.name || a.node_id || '')
            .localeCompare(String(b.clean_name || b.name || b.node_id || ''), undefined, { sensitivity: 'base' });
    }

    function byLastHeardDesc(a, b) {
        return nodeLastSeen(b) - nodeLastSeen(a);
    }

    // context additionally: { distance(node) -> meters | null }
    function sortNodes(nodes, sortKey, context) {
        const ctx = context || {};
        const list = nodes.slice();
        const key = SORT_KEYS.indexOf(sortKey) !== -1 ? sortKey : 'last_heard';
        const stable = function (cmp) {
            // Ties fall back to last heard, then name, so order never flickers.
            return function (a, b) { return cmp(a, b) || byLastHeardDesc(a, b) || compareNames(a, b); };
        };
        if (key === 'last_heard') {
            list.sort(stable(function () { return 0; }));
        } else if (key === 'name') {
            list.sort(function (a, b) { return compareNames(a, b) || byLastHeardDesc(a, b); });
        } else if (key === 'distance') {
            const cache = new Map();
            const dist = function (n) {
                if (!cache.has(n)) {
                    const d = typeof ctx.distance === 'function' ? ctx.distance(n) : null;
                    cache.set(n, Number.isFinite(d) ? d : null);
                }
                return cache.get(n);
            };
            list.sort(stable(function (a, b) {
                const da = dist(a); const db = dist(b);
                if (da === null && db === null) return 0;
                if (da === null) return 1;      // no position -> last
                if (db === null) return -1;
                return da - db;
            }));
        } else if (key === 'hops') {
            list.sort(stable(function (a, b) {
                const ha = nodeHops(a); const hb = nodeHops(b);
                if (ha === null && hb === null) return 0;
                if (ha === null) return 1;
                if (hb === null) return -1;
                return ha - hb;
            }));
        } else if (key === 'favorites_first') {
            list.sort(stable(function (a, b) {
                return (b.favorite ? 1 : 0) - (a.favorite ? 1 : 0);
            }));
        }
        return list;
    }

    function filterAndSortNodes(nodes, state, context) {
        const s = normalizeNodeFilters(state);
        const kept = (nodes || []).filter(function (node) { return nodePassesFilters(node, s, context); });
        return sortNodes(kept, s.sort, context);
    }

    // ---------------- Message click -> sender's node card ----------------

    // Elements inside a bubble that have their own click meaning; a click on
    // (or inside) any of them must never also trigger "focus sender's node".
    const INTERACTIVE_SELECTOR = [
        '.message-actions-trigger', '.message-reply-quote', '.message-retry-btn',
        'a', 'button', 'input', 'textarea', 'select', 'summary', '[contenteditable]',
    ].join(',');

    // target: the clicked element (anything with .closest). hasSelection: the
    // user has text selected (they were selecting, not clicking).
    function isPlainMessageClick(target, hasSelection) {
        if (hasSelection) return false;
        if (!target || typeof target.closest !== 'function') return false;
        return !target.closest(INTERACTIVE_SELECTOR);
    }

    // Which node sent `message`? Own messages -> the local node; received ones
    // -> their node_id; system notices and anything unresolvable -> null.
    function resolveSenderNodeId(message, context) {
        if (!message || message.kind === 'system') return null;
        const ctx = context || {};
        const isOwn = typeof ctx.isOwn === 'function' ? ctx.isOwn(message) : message.kind === 'me';
        const id = String(isOwn ? (ctx.localNodeId || '') : (message.node_id || '')).trim();
        return /^![0-9a-fA-F]{8}$/.test(id) ? id.toLowerCase() : null;
    }

    // What to do for a sender: scroll to the card, tell the user a filter hides
    // it, or tell them the node is unknown. visibleIds = ids currently rendered
    // in the list (filters + search applied); knownIds = every node we have.
    function planSenderFocus(nodeId, visibleIds, knownIds) {
        if (!nodeId) return 'not_found';
        if (visibleIds && visibleIds.has(nodeId)) return 'focus';
        if (knownIds && knownIds.has(nodeId)) return 'hidden_by_filter';
        return 'not_found';
    }

    // Minimal filter state that lets `node` show up: keep the sort, drop every
    // filter, and (since ignored nodes only appear through it) turn on
    // "ignored only" for an ignored node.
    function stateRevealingNode(state, node) {
        const next = normalizeNodeFilters(Object.assign({}, DEFAULT_NODE_FILTERS, { sort: normalizeNodeFilters(state).sort }));
        if (node && node.ignored) next.ignoredOnly = true;
        return next;
    }

    const api = {
        isPlainMessageClick: isPlainMessageClick,
        resolveSenderNodeId: resolveSenderNodeId,
        planSenderFocus: planSenderFocus,
        stateRevealingNode: stateRevealingNode,
        formatMessageTime: formatMessageTime,
        firstUnreadIndex: firstUnreadIndex,
        SORT_KEYS: SORT_KEYS,
        FILTER_FLAGS: FILTER_FLAGS,
        DEFAULT_NODE_FILTERS: DEFAULT_NODE_FILTERS,
        OFFLINE_AFTER_SECONDS: OFFLINE_AFTER_SECONDS,
        INFRASTRUCTURE_ROLES: INFRASTRUCTURE_ROLES,
        normalizeNodeFilters: normalizeNodeFilters,
        activeFilterCount: activeFilterCount,
        nodeHops: nodeHops,
        nodePassesFilters: nodePassesFilters,
        sortNodes: sortNodes,
        filterAndSortNodes: filterAndSortNodes,
    };
    root.MCViews = api;
})(typeof window !== 'undefined' ? window : globalThis);
