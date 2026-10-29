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
    const DIRECT_WITHIN_SECONDS = 24 * 60 * 60;
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

    // NL1: "heard directly" = its own packet arrived with no relay hop in the last 24 h. `last_direct` (epoch seconds,
    // noted by the ingest paths since SIG4) is the truth; a node that has none yet falls back to hops_away == 0 AND
    // heard at all in the last 24 h. One rule for the filter, the header pill and its number.
    function isHeardDirectly(node, now) {
        const current = Number(now);
        const withinDay = function (seconds) {
            return Number.isFinite(seconds) && seconds > 0 && (!Number.isFinite(current) || current - seconds <= DIRECT_WITHIN_SECONDS);
        };
        const direct = node ? node.last_direct : undefined;
        if (direct !== undefined && direct !== null && direct !== '') return withinDay(Number(direct));
        return nodeHops(node) === 0 && withinDay(nodeLastSeen(node));
    }

    function isOnline(node, now) {
        const seen = nodeLastSeen(node);
        const current = Number(now);
        return Boolean(seen) && (!Number.isFinite(current) || current - seen <= OFFLINE_AFTER_SECONDS);
    }

    // NL1: the three numbers of the Nodes header, over the nodes that are not ignored (whatever the other filters and
    // the search say). `shown` is the length of the list the user currently sees (filters + search applied).
    function nodeHeaderCounts(nodes, shown, now) {
        const list = (Array.isArray(nodes) ? nodes : []).filter(function (n) { return n && !n.ignored; });
        let online = 0;
        let direct = 0;
        list.forEach(function (n) {
            if (isOnline(n, now)) online += 1;
            if (isHeardDirectly(n, now)) direct += 1;
        });
        const total = list.length;
        const shownCount = Number.isFinite(Number(shown)) ? Number(shown) : total;
        return { total: total, shown: shownCount, filtered: shownCount !== total, online: online, direct: direct };
    }

    // NL1: the Nodes header always MEASURES the full labels, whatever form it shows now (a decision never feeds the next
    // one, so it cannot stick): put the row into the full form, take its natural width, compare it with the width the
    // row really has, then choose. `env.available(row)` is the row's content width, `env.natural(row)` the natural width
    // of the full form: the visible children and the gaps between them, the reserved x slot only when the x is shown (the
    // transparent touch hit zones are never part of it). Returns the form it set, or null when the row cannot be
    // measured (hidden pane).
    function fitNodesHeaderRow(row, env) {
        if (!row || !env) return null;
        const available = Number(env.available(row));
        if (!Number.isFinite(available) || available <= 0) return null;
        row.setAttribute('data-fit', 'full');
        const natural = Number(env.natural(row));
        const mode = Number.isFinite(natural) && natural > available + 0.5 ? 'compact' : 'full';
        row.setAttribute('data-fit', mode);
        return mode;
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
        if (s.directOnly && !isHeardDirectly(node, ctx.now)) return false;
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

    // MOB2-D: the phone renders a window of the node list (638 cards = 2240 tap targets). `nodes` is the list as
    // the user sees it (filters, search and sort already applied). With the default sort the window starts with
    // the own node, then the favourites, then the most recently heard (the list's own order); any other sort is
    // the user's choice and keeps its order. opts: limit (default PHONE_NODE_BATCH), sort, localNodeId,
    // mustInclude (ids that must be in the window: the selected node, a node focused from a message).
    const PHONE_NODE_BATCH = 60;

    function phoneNodeWindow(nodes, opts) {
        const o = opts || {};
        const list = Array.isArray(nodes) ? nodes.slice() : [];
        const total = list.length;
        let ordered = list;
        if ((o.sort || 'last_heard') === 'last_heard') {
            const rank = function (node) {
                if (node.is_local || (o.localNodeId && node.node_id === o.localNodeId)) return 0;
                return node.favorite ? 1 : 2;
            };
            ordered = list
                .map(function (node, index) { return { node: node, index: index, rank: rank(node) }; })
                .sort(function (a, b) { return a.rank - b.rank || a.index - b.index; })
                .map(function (entry) { return entry.node; });
        }
        let limit = Math.max(1, Math.floor(Number(o.limit)) || PHONE_NODE_BATCH);
        (Array.isArray(o.mustInclude) ? o.mustInclude : []).forEach(function (id) {
            const at = ordered.findIndex(function (node) { return node.node_id === id; });
            if (at >= limit) limit = at + 1;
        });
        const shown = ordered.slice(0, limit);
        return { nodes: shown, hidden: total - shown.length, total: total };
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

    // ---------------- Map / node-list interaction (U3) ----------------

    // The one list of per-node actions, shared by the map's right-click menu,
    // the map popup's buttons and (where it applies) the node card, so they can
    // never drift apart. Items map 1:1 onto functions that already exist
    // (openChat, toggleFavorite, toggleIgnore, openCreateWaypointDialog,
    // runNodeTool, setNodeAsReference, copyCoordinates, ...). ctx:
    //   hasPosition - the node has coordinates
    //   canRequestKey - the target store says a key request may be sent
    //   keyUnknown - trust_state is "unknown"
    function nodeActionItems(node, ctx) {
        const c = ctx || {};
        const items = [{ id: 'message' }];
        items.push({ id: 'favorite', on: Boolean(node && node.favorite) });
        items.push({ id: 'ignore', on: Boolean(node && node.ignored) });
        if (c.hasPosition) {
            items.push({ id: 'waypoint_here' });
            items.push({ id: 'center' });
        }
        if (c.keyUnknown && c.canRequestKey) items.push({ id: 'request_key' });
        items.push({ id: 'request_telemetry' });
        items.push({ id: 'request_position' });
        items.push({ id: 'traceroute' });
        if (c.isReference) {
            // The node IS the reference point: offer to clear it instead.
            items.push({ id: 'clear_reference' });
        } else if (c.hasPosition) {
            items.push({ id: 'set_reference' });
        }
        if (c.hasPosition) items.push({ id: 'copy_coordinates' });
        items.push({ id: 'details' });
        return items;
    }

    // A reference point that is a plain location (not a node) has no node
    // actions - just these three.
    function referenceLocationItems() {
        return [{ id: 'copy_coordinates' }, { id: 'change_reference' }, { id: 'waypoint_here' }];
    }

    // Where to put a context menu so it stays inside the map: prefer the
    // bottom-right of the pointer, flip to the other side when it would
    // overflow, then clamp; a menu taller than the space gets maxHeight and
    // scrolls. point/size/bounds are in container pixels.
    function placeContextMenu(point, size, bounds, margin) {
        const m = margin === undefined ? 6 : margin;
        const availW = Math.max(0, bounds.width - 2 * m);
        const availH = Math.max(0, bounds.height - 2 * m);
        const width = Math.min(size.width, availW);
        const height = Math.min(size.height, availH);
        let left = point.x;
        let top = point.y;
        if (left + width > bounds.width - m) left = point.x - width;   // flip left of the pointer
        if (top + height > bounds.height - m) top = point.y - height;  // flip above the pointer
        left = Math.max(m, Math.min(left, bounds.width - m - width));
        top = Math.max(m, Math.min(top, bounds.height - m - height));
        return { left: left, top: top, maxHeight: availH };
    }

    // A "scroll the entry into view" request that was already issued: is its
    // smooth scroll still under way? Then a second pass (after a re-render) must
    // leave it alone instead of restarting it. In progress = started recently
    // and the list has actually moved since it started.
    function followScrollInProgress(follow, nowMs, scrollTop) {
        if (!follow || !follow.scrolled) return false;
        if (nowMs - follow.scrolledAt > 1500) return false;
        return Math.abs(scrollTop - follow.startTop) > 1;
    }

    // Single vs double click from successive click events. Markers get rebuilt
    // on every selection, so the browser's own dblclick (which needs the same
    // element twice) cannot be relied on - two clicks on the same node within
    // `windowMs` are a double click. Returns {kind, next}; keep `next` as state.
    function classifyNodeClick(previous, nodeId, nowMs, windowMs) {
        const limit = windowMs === undefined ? 350 : windowMs;
        if (previous && previous.nodeId === nodeId && nowMs - previous.at <= limit && !previous.consumed) {
            return { kind: 'double', next: { nodeId: nodeId, at: nowMs, consumed: true } };
        }
        return { kind: 'single', next: { nodeId: nodeId, at: nowMs, consumed: false } };
    }

    // Long-press detector for touch (and anything else lacking a native
    // contextmenu): fires once if a single pointer stays within `tolerancePx`
    // of where it went down for `delayMs`. A second pointer (pinch), movement
    // beyond the tolerance (pan) or an early release cancels it. Timers are
    // injected so the logic is testable without a clock.
    function createLongPressDetector(options) {
        const o = options || {};
        const delayMs = o.delayMs === undefined ? 500 : o.delayMs;
        const tolerancePx = o.tolerancePx === undefined ? 10 : o.tolerancePx;
        const setTimer = o.setTimer;
        const clearTimer = o.clearTimer;
        let timer = null;
        let origin = null;
        let fired = false;

        function cancel() {
            if (timer !== null) { clearTimer(timer); timer = null; }
            origin = null;
        }
        return {
            start: function (point, pointerCount) {
                cancel();
                fired = false;
                if (pointerCount > 1) return;
                origin = { x: point.x, y: point.y };
                timer = setTimer(function () {
                    timer = null;
                    fired = true;
                    const at = origin;
                    origin = null;
                    if (typeof o.onLongPress === 'function') o.onLongPress(at);
                }, delayMs);
            },
            move: function (point, pointerCount) {
                if (origin === null) return;
                if (pointerCount > 1) { cancel(); return; }
                const dx = point.x - origin.x;
                const dy = point.y - origin.y;
                if (Math.sqrt(dx * dx + dy * dy) > tolerancePx) cancel();
            },
            end: function () {
                const wasFired = fired;
                cancel();
                fired = false;
                return wasFired; // true: the release that follows a long press (swallow the click)
            },
            cancel: cancel,
            isPending: function () { return timer !== null; },
        };
    }

    // The chat list is re-rendered (innerHTML) right after a DM is opened, which
    // can drop the scroll that was just started. After each render: scroll the
    // active entry in if a recent open asked for it and it is still not fully
    // visible; keep waiting while it has not been rendered yet; give up after
    // `ttlMs`. follow = {id, at} | null.
    function chatListFollowAction(follow, nowMs, entryExists, entryFullyVisible, ttlMs) {
        const ttl = ttlMs === undefined ? 8000 : ttlMs;
        if (!follow || !follow.id) return 'none';
        if (nowMs - follow.at > ttl) return 'expire';
        if (!entryExists) return 'wait';
        return entryFullyVisible ? 'done' : 'scroll';
    }

    // ---------------- Mark-as-read decisions (P2) ----------------
    // GET /api/messages no longer marks anything read; the page asks explicitly
    // (POST /api/messages/mark_read) - but only when there is something to mark,
    // never on every 2 s poll, once at a time per chat, and not again for a
    // while after a failure. Pure state transitions so they can be tested.

    const MARK_READ_BACKOFF_MS = 30000;

    function newMarkReadState() {
        // lastKey: key of the newest incoming message already accounted for
        // (null until the first load of the chat); inFlight: one request per chat.
        return { lastKey: null, inFlight: false, backoffUntil: 0 };
    }

    // The key of the newest incoming (kind "rx") message, '' when there is none.
    function newestIncomingKey(messages) {
        const list = Array.isArray(messages) ? messages : [];
        for (let i = list.length - 1; i >= 0; i -= 1) {
            const m = list[i];
            if (m && m.kind === 'rx') return String(m.id || m.packet_id || i);
        }
        return '';
    }

    // -> { mark: boolean, state: newState }. input: { unread, newestRxKey, now }
    function decideMarkRead(state, input) {
        const s = Object.assign(newMarkReadState(), state || {});
        const unread = Math.max(0, Math.floor(Number(input.unread)) || 0);
        const key = String(input.newestRxKey || '');
        if (s.inFlight) return { mark: false, state: s };
        if (input.now < s.backoffUntil) return { mark: false, state: s };
        if (unread > 0) return { mark: true, state: s };
        if (s.lastKey === null) {
            // First load of a chat that is already read: just remember where we are.
            return { mark: false, state: Object.assign({}, s, { lastKey: key }) };
        }
        if (key && key !== s.lastKey) return { mark: true, state: s };
        return { mark: false, state: s };
    }

    function markReadStarted(state) {
        return Object.assign({}, state, { inFlight: true });
    }

    function markReadSucceeded(state, newestRxKey) {
        return Object.assign({}, state, { inFlight: false, lastKey: String(newestRxKey || ''), backoffUntil: 0 });
    }

    function markReadFailed(state, now) {
        return Object.assign({}, state, { inFlight: false, backoffUntil: now + MARK_READ_BACKOFF_MS });
    }

    // RR1: while the radio is released the composer cannot send - say why instead of letting
    // the user type into a send that is bound to fail. `released` is the /api/radio_health
    // `released` object (null/undefined when not released); `t` translates a key.
    function releasedComposerState(released, t) {
        if (!released) return { blocked: false, placeholder: null, hint: '' };
        return {
            blocked: true,
            placeholder: t('chat.radio_released_placeholder'),
            hint: t('chat.radio_released_hint'),
        };
    }

    // v1.12.2: the Node Manager profile card's connection badge. A released radio (serial or TCP)
    // is a deliberate, neutral state - it used to read "Offline" in the warning colour.
    function profileConnectionBadge(connection) {
        const c = connection || {};
        if (c.mode === 'released' || c.mode === 'releasing') {
            return { state: 'released', labelKey: 'settings.radio_status_released' };
        }
        if (c.listener_running && c.mode === 'connected') {
            return { state: 'connected', labelKey: 'settings.radio_status_connected' };
        }
        return { state: 'offline', labelKey: 'node_manager.badge_offline' };
    }

    // ---- SIG1: literal "\n" in received texts -------------------------------------------------
    // Some stations (automatic responders) send the two characters backslash + n instead of a real
    // newline. DISPLAY ONLY (the stored text is never changed; Copy keeps the original): a literal
    // "\n" / "\r\n" becomes a real line break, real CR LF is normalised. Callers escape the result
    // (escapeHtml / textContent) - this function never builds HTML.
    function expandEscapedNewlines(text) {
        return String(text === null || text === undefined ? '' : text)
            .replace(/\\r\\n|\\n/g, '\n')
            .replace(/\r\n/g, '\n');
    }

    // One-line previews (chat list, browser notifications, reply composer): line breaks become " · ".
    function previewText(text) {
        return expandEscapedNewlines(text).split('\n').map((part) => part.trim()).filter(Boolean).join(' · ');
    }

    // ---- SIG1: "Reply with signal" -----------------------------------------------------------
    function finiteNumber(value) {
        if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
        const number = Number(value);
        return Number.isFinite(number) ? number : null;
    }

    function pad2(n) {
        return n < 10 ? '0' + n : String(n);
    }

    // HH:MM:SS of the message's receive time (epoch `ts` in the browser's local time, else the stored string).
    function signalReplyTime(message) {
        const ts = finiteNumber(message && message.ts);
        if (ts !== null && ts > 0) {
            const d = new Date(ts * 1000);
            return pad2(d.getHours()) + ':' + pad2(d.getMinutes()) + ':' + pad2(d.getSeconds());
        }
        const stored = String((message && message.time) || '').trim();
        return /^\d{1,2}:\d{2}:\d{2}$/.test(stored) ? stored : '';
    }

    function formatSignalNumber(value) {
        return String(Math.round(value * 100) / 100);
    }

    // The text of "Reply with signal", or null when the message has neither SNR nor RSSI. Language-neutral,
    // real newlines, no names (emoji names blow the byte budget):
    //   Rx 16:28:06\nSNR -5.5 dB | RSSI -87 dBm\nHops 3
    function buildSignalReplyText(message) {
        const rx = (message && message.rx && typeof message.rx === 'object') ? message.rx : {};
        const snr = finiteNumber(rx.snr);
        const rssi = finiteNumber(rx.rssi);
        if (snr === null && rssi === null) return null;

        const lines = [];
        const time = signalReplyTime(message);
        if (time) lines.push('Rx ' + time);

        const signal = [];
        if (snr !== null) signal.push('SNR ' + formatSignalNumber(snr) + ' dB');
        if (rssi !== null) signal.push('RSSI ' + Math.round(rssi) + ' dBm');
        lines.push(signal.join(' | '));

        // hop_start 0 means "unknown" in the protocol (older firmware), so it is not a hop count.
        const hopStart = finiteNumber(rx.hop_start);
        const hopLimit = finiteNumber(rx.hop_limit);
        if (hopStart !== null && hopLimit !== null && hopStart > 0 && hopStart >= hopLimit) {
            const hops = hopStart - hopLimit;
            lines.push(hops === 0 ? 'Hops 0 (direct)' : 'Hops ' + hops);
        }
        // SIG4: "Via <short name>" only when the relay is resolved to exactly one node that was heard DIRECTLY
        // (a guess from the 2 h fallback never goes on air); left out when it does not fit the byte budget.
        const via = relayViaName(rx);
        if (via && utf8Length(lines.concat('Via ' + via).join('\n')) <= SIGNAL_REPLY_MAX_BYTES) lines.push('Via ' + via);
        return lines.join('\n');
    }

    const SIGNAL_REPLY_MAX_BYTES = 120;

    // The short name of a RESOLVED relay as plain ASCII (emoji names blow the budget), or '' for any other status.
    function relayViaName(rx) {
        const relay = rx && rx.relay;
        if (!relay || relay.status !== 'resolved' || relay.basis !== 'direct24h') return '';
        return String(relay.short_name || '').replace(/[^\x20-\x7e]/g, '').trim().slice(0, 8);
    }

    // What Message Info shows for the last relay of one packet. kind: none (no byte), direct, resolved, probably
    // (resolved from the 2 h fallback only), ambiguous, unknown; `hex` is the raw byte as 0x84; `names` the short names (resolved: one, ambiguous: up to four).
    function relayInfo(rx) {
        const byte = finiteNumber(rx && rx.relay_node);
        if (byte === null || byte < 1 || byte > 255) return { kind: 'none', hex: '', names: [], count: 0 };
        const hex = '0x' + Math.round(byte).toString(16).toUpperCase().padStart(2, '0');
        const relay = (rx && rx.relay) || {};
        const label = function (entry) { return String((entry && (entry.short_name || entry.node_id)) || '').trim(); };
        if (relay.status === 'resolved' && relay.basis === 'recent2h') {
            // one node heard in the last 2 hours, but never directly: a probable relay, not a confirmed one
            return { kind: 'probably', hex: hex, names: [label(relay)].filter(Boolean), count: 1 };
        }
        if (relay.status === 'direct' || relay.status === 'resolved') {
            return { kind: relay.status, hex: hex, names: [label(relay)].filter(Boolean), count: 1 };
        }
        if (relay.status === 'ambiguous') {
            const names = (Array.isArray(relay.candidates) ? relay.candidates : []).map(label).filter(Boolean);
            return { kind: 'ambiguous', hex: hex, names: names, count: finiteNumber(relay.count) || names.length };
        }
        return { kind: 'unknown', hex: hex, names: [], count: 0 };
    }

    // SIG4: menu state for "Trace route to sender". Hidden for own/system messages and messages without a real node
    // sender; disabled (with a reason key the caller translates) on a TCP radio, a released radio, while a trace is
    // running or inside the repeat block. options: isOwn, transportType, released, busy, cooldownLeftS.
    function tracerouteMenuState(message, options) {
        const opts = options || {};
        const hidden = { visible: false, disabled: false, reasonKey: '', retryS: 0 };
        if (!message || message.kind !== 'rx' || opts.isOwn) return hidden;
        if (!/^![0-9a-fA-F]{8}$/.test(String(message.node_id || ''))) return hidden;
        const show = function (reasonKey, retryS) { return { visible: true, disabled: Boolean(reasonKey), reasonKey: reasonKey || '', retryS: retryS || 0 }; };
        if (String(opts.transportType || '').toLowerCase() === 'tcp') return show('tcp');
        if (opts.released) return show('released');
        if (opts.busy) return show('busy');
        const left = finiteNumber(opts.cooldownLeftS);
        if (left !== null && left > 0) return show('cooldown', Math.ceil(left));
        return show('');
    }

    function utf8Length(text) {
        if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(text).length;
        return unescape(encodeURIComponent(text)).length;
    }

    // Menu state for the "Reply with signal" item. `isOwn`: our own radio sent it; `releasedReason`: the
    // composer's reason text when the radio is released (the item is then shown but disabled).
    function signalReplyMenuState(message, options) {
        const opts = options || {};
        const hidden = { visible: false, disabled: false, text: null, reason: '' };
        if (!message || message.kind !== 'rx' || opts.isOwn) return hidden;
        if (finiteNumber(message.packet_id) === null) return hidden; // a native reply needs the packet id
        const text = buildSignalReplyText(message);
        if (text === null) return hidden;
        const released = Boolean(opts.releasedReason);
        return { visible: true, disabled: released, text: text, reason: released ? String(opts.releasedReason) : '' };
    }

    const api = {
        newMarkReadState: newMarkReadState,
        newestIncomingKey: newestIncomingKey,
        decideMarkRead: decideMarkRead,
        markReadStarted: markReadStarted,
        markReadSucceeded: markReadSucceeded,
        markReadFailed: markReadFailed,
        MARK_READ_BACKOFF_MS: MARK_READ_BACKOFF_MS,
        nodeActionItems: nodeActionItems,
        referenceLocationItems: referenceLocationItems,
        placeContextMenu: placeContextMenu,
        followScrollInProgress: followScrollInProgress,
        classifyNodeClick: classifyNodeClick,
        createLongPressDetector: createLongPressDetector,
        chatListFollowAction: chatListFollowAction,
        isPlainMessageClick: isPlainMessageClick,
        resolveSenderNodeId: resolveSenderNodeId,
        planSenderFocus: planSenderFocus,
        stateRevealingNode: stateRevealingNode,
        formatMessageTime: formatMessageTime,
        releasedComposerState: releasedComposerState,
        expandEscapedNewlines: expandEscapedNewlines,
        previewText: previewText,
        buildSignalReplyText: buildSignalReplyText,
        signalReplyMenuState: signalReplyMenuState,
        relayInfo: relayInfo,
        tracerouteMenuState: tracerouteMenuState,
        utf8Length: utf8Length,
        profileConnectionBadge: profileConnectionBadge,
        firstUnreadIndex: firstUnreadIndex,
        SORT_KEYS: SORT_KEYS,
        FILTER_FLAGS: FILTER_FLAGS,
        DEFAULT_NODE_FILTERS: DEFAULT_NODE_FILTERS,
        OFFLINE_AFTER_SECONDS: OFFLINE_AFTER_SECONDS,
        INFRASTRUCTURE_ROLES: INFRASTRUCTURE_ROLES,
        normalizeNodeFilters: normalizeNodeFilters,
        activeFilterCount: activeFilterCount,
        nodeHops: nodeHops,
        isHeardDirectly: isHeardDirectly,
        isOnline: isOnline,
        nodeHeaderCounts: nodeHeaderCounts,
        fitNodesHeaderRow: fitNodesHeaderRow,
        DIRECT_WITHIN_SECONDS: DIRECT_WITHIN_SECONDS,
        nodePassesFilters: nodePassesFilters,
        sortNodes: sortNodes,
        filterAndSortNodes: filterAndSortNodes,
        phoneNodeWindow: phoneNodeWindow,
        PHONE_NODE_BATCH: PHONE_NODE_BATCH,
    };
    root.MCViews = api;
})(typeof window !== 'undefined' ? window : globalThis);
