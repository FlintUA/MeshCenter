// tests/frontend/test_chat_views.mjs
//
// U1: behavior tests for static/chat-views.js (message timestamp formatter,
// first-unread index, node sort/filter predicates). Loads the REAL source
// under node's vm - no DOM, no npm. Run with:
//
//   node tests/frontend/test_chat_views.mjs

process.env.TZ = 'Europe/Berlin';

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const source = readFileSync(path.join(__dirname, '..', '..', 'static', 'chat-views.js'), 'utf8');
const sandbox = { Intl, Date, Math, Number, String, Object, Array, Map };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const V = sandbox.MCViews;
assert.ok(V, 'MCViews exported');

const tr = (key) => ({ 'chat.date_yesterday': 'yesterday' }[key] || key);
const local = (y, mo, d, h = 0, mi = 0, s = 0) => new Date(y, mo - 1, d, h, mi, s).getTime() / 1000;
const NOW = local(2026, 10, 10, 12, 0, 0) * 1000; // Saturday 10 Oct 2026 12:00 local
const fmt = (ts, locale = 'de') => V.formatMessageTime(ts, { now: NOW, locale, t: tr });

let passed = 0;
function test(name, fn) {
    fn();
    passed += 1;
}

// ---------------------------------------------------------------- formatter
test('today shows time only', () => {
    assert.equal(fmt(local(2026, 10, 10, 18, 36)), '18:36');
    assert.equal(fmt(local(2026, 10, 10, 9, 5)), '09:05');
});

test('yesterday gets the translated prefix', () => {
    assert.equal(fmt(local(2026, 10, 9, 18, 36)), 'yesterday 18:36');
});

test('midnight edges: 00:00:00 today is today, 23:59:59 the day before is yesterday', () => {
    assert.equal(fmt(local(2026, 10, 10, 0, 0, 0)), '00:00');
    assert.equal(fmt(local(2026, 10, 9, 23, 59, 59)), 'yesterday 23:59');
    assert.equal(fmt(local(2026, 10, 9, 0, 0, 0)), 'yesterday 00:00');
    assert.equal(fmt(local(2026, 10, 8, 23, 59, 59)).includes('yesterday'), false);
});

test('"now" just after midnight still treats the previous evening as yesterday', () => {
    const justAfterMidnight = local(2026, 10, 10, 0, 0, 5) * 1000;
    const out = V.formatMessageTime(local(2026, 10, 9, 23, 59, 50), { now: justAfterMidnight, locale: 'de', t: tr });
    assert.equal(out, 'yesterday 23:59');
    assert.equal(V.formatMessageTime(local(2026, 10, 10, 0, 0, 1), { now: justAfterMidnight, locale: 'de', t: tr }), '00:00');
});

test('yesterday across a month and a year boundary', () => {
    const jan1 = local(2027, 1, 1, 0, 30) * 1000;
    assert.equal(V.formatMessageTime(local(2026, 12, 31, 22, 0), { now: jan1, locale: 'de', t: tr }), 'yesterday 22:00');
    const mar1 = local(2026, 3, 1, 8, 0) * 1000;
    assert.equal(V.formatMessageTime(local(2026, 2, 28, 22, 0), { now: mar1, locale: 'de', t: tr }), 'yesterday 22:00');
});

test('DST change day does not shift the calendar day (Berlin, 2026-03-29)', () => {
    const afterSwitch = local(2026, 3, 29, 15, 0) * 1000;
    assert.equal(V.formatMessageTime(local(2026, 3, 28, 23, 30), { now: afterSwitch, locale: 'de', t: tr }), 'yesterday 23:30');
    assert.equal(V.formatMessageTime(local(2026, 3, 29, 1, 30), { now: afterSwitch, locale: 'de', t: tr }), '01:30');
});

test('earlier this year: day.month + time, locale order', () => {
    assert.equal(fmt(local(2026, 8, 10, 18, 36), 'ru'), '10.08 18:36');
    assert.equal(fmt(local(2026, 8, 10, 18, 36), 'uk'), '10.08 18:36');
    assert.equal(fmt(local(2026, 8, 10, 18, 36), 'de'), '10.08. 18:36');
    assert.equal(fmt(local(2026, 8, 10, 18, 36), 'en'), '10/08 18:36');
});

test('previous years include the year', () => {
    assert.equal(fmt(local(2025, 10, 8, 18, 36), 'ru'), '08.10.2025 18:36');
    assert.equal(fmt(local(2025, 10, 8, 18, 36), 'de'), '08.10.2025 18:36');
});

test('hour 24 never appears (h23)', () => {
    assert.ok(!fmt(local(2026, 10, 10, 0, 7)).startsWith('24'));
});

test('missing / invalid ts yields empty string so the caller shows the legacy time', () => {
    for (const bad of [undefined, null, '', 0, -5, 'abc', NaN]) {
        assert.equal(fmt(bad), '');
    }
});

test('ts given as a numeric string works (JSON round-trips)', () => {
    assert.equal(fmt(String(local(2026, 10, 10, 18, 36))), '18:36');
});

// ------------------------------------------------------------ first unread
test('firstUnreadIndex: N-th message from the end', () => {
    assert.equal(V.firstUnreadIndex(10, 3), 7);
    assert.equal(V.firstUnreadIndex(10, 1), 9);
    assert.equal(V.firstUnreadIndex(10, 10), 0);
});

test('firstUnreadIndex: N larger than loaded clamps to the oldest', () => {
    assert.equal(V.firstUnreadIndex(4, 50), 0);
});

test('firstUnreadIndex: N=0, bad values or empty chat mean no divider', () => {
    assert.equal(V.firstUnreadIndex(10, 0), -1);
    assert.equal(V.firstUnreadIndex(10, -2), -1);
    assert.equal(V.firstUnreadIndex(10, undefined), -1);
    assert.equal(V.firstUnreadIndex(10, 'x'), -1);
    assert.equal(V.firstUnreadIndex(0, 3), -1);
});

// ------------------------------------------------------------ node filters
const NOW_S = 1_800_000_000;
const node = (id, extra = {}) => ({
    node_id: id, clean_name: id, role: 'CLIENT', last_seen: NOW_S - 60,
    hop_start: '1', favorite: false, ignored: false, position: null, ...extra,
});
const NODES = [
    node('!aaaa0001', { clean_name: 'Zulu', favorite: true, hop_start: '0', last_seen: NOW_S - 30 }),
    node('!aaaa0002', { clean_name: 'alpha', hop_start: '2', last_seen: NOW_S - 600 }),
    node('!aaaa0003', { clean_name: 'Bravo', ignored: true, hop_start: '0', last_seen: NOW_S - 5 }),
    node('!aaaa0004', { clean_name: 'Router1', role: 'ROUTER', hop_start: '1', last_seen: NOW_S - 100 }),
    node('!aaaa0005', { clean_name: 'Late', role: 'ROUTER_LATE', last_seen: NOW_S - 200 }),
    node('!aaaa0006', { clean_name: 'Rep', role: 'REPEATER', last_seen: NOW_S - 300 }),
    node('!aaaa0007', { clean_name: 'Old', last_seen: NOW_S - 3 * 3600, hop_start: '' }),
    node('!aaaa0008', { clean_name: 'NoSeen', last_seen: 0 }),
    node('!aaaa0009', { clean_name: 'Tracker', role: 'TRACKER', hop_start: '3', last_seen: NOW_S - 400, favorite: true, ignored: true }),
];
const KNOWN = new Set(['!aaaa0001', '!aaaa0002']);
const ctx = {
    now: NOW_S,
    hasKnownKey: (n) => KNOWN.has(n.node_id),
    distance: (n) => ({ '!aaaa0001': 5000, '!aaaa0002': 100, '!aaaa0004': 100 }[n.node_id] ?? null),
};
const ids = (state) => V.filterAndSortNodes(NODES, state, ctx).map((n) => n.node_id);

test('DEFAULT hides ignored nodes and sorts by last heard', () => {
    const out = ids(null);
    assert.ok(!out.includes('!aaaa0003'));
    assert.ok(!out.includes('!aaaa0009'));
    assert.equal(out[0], '!aaaa0001');
    assert.equal(out[out.length - 1], '!aaaa0008'); // never seen -> last
    assert.equal(out.length, NODES.length - 2);
});

test('ignored only shows exactly the ignored nodes', () => {
    assert.deepEqual(ids({ ignoredOnly: true }).sort(), ['!aaaa0003', '!aaaa0009']);
});

test('favorites only (ignored stay hidden) and favorites+ignored combine with AND', () => {
    assert.deepEqual(ids({ favoritesOnly: true }), ['!aaaa0001']);
    assert.deepEqual(ids({ favoritesOnly: true, ignoredOnly: true }), ['!aaaa0009']);
});

test('hide offline drops nodes unheard for more than 2 h and never-seen ones', () => {
    const out = ids({ hideOffline: true });
    assert.ok(!out.includes('!aaaa0007'));
    assert.ok(!out.includes('!aaaa0008'));
    assert.ok(out.includes('!aaaa0002'));
    // exactly on the limit is still online
    const edge = [node('!edge0001', { last_seen: NOW_S - 7200 }), node('!edge0002', { last_seen: NOW_S - 7201 })];
    assert.deepEqual(V.filterAndSortNodes(edge, { hideOffline: true }, ctx).map((n) => n.node_id), ['!edge0001']);
});

test('heard directly only keeps only known 0-hop nodes', () => {
    assert.deepEqual(ids({ directOnly: true }), ['!aaaa0001']); // unknown hops ("") are not 0
});

test('hide infrastructure removes ROUTER / ROUTER_LATE / REPEATER (any case)', () => {
    const out = ids({ hideInfrastructure: true });
    for (const id of ['!aaaa0004', '!aaaa0005', '!aaaa0006']) assert.ok(!out.includes(id), id);
    assert.ok(out.includes('!aaaa0002'));
    const lower = [node('!low00001', { role: 'router' })];
    assert.deepEqual(V.filterAndSortNodes(lower, { hideInfrastructure: true }, ctx), []);
    const tracker = [node('!trk00001', { role: 'TRACKER' }), node('!cli00001', { role: 'CLIENT_MUTE' })];
    assert.equal(V.filterAndSortNodes(tracker, { hideInfrastructure: true }, ctx).length, 2);
});

test('known key only uses the supplied predicate (no predicate -> nothing passes)', () => {
    assert.deepEqual(ids({ knownKeyOnly: true }).sort(), ['!aaaa0001', '!aaaa0002']);
    assert.deepEqual(V.filterAndSortNodes(NODES, { knownKeyOnly: true }, { now: NOW_S }), []);
});

test('filters are ANDed', () => {
    assert.deepEqual(ids({ favoritesOnly: true, directOnly: true, knownKeyOnly: true, hideOffline: true }), ['!aaaa0001']);
    assert.deepEqual(ids({ directOnly: true, hideInfrastructure: true, knownKeyOnly: true, favoritesOnly: false, hideOffline: true }), ['!aaaa0001']);
});

test('sort: name is case-insensitive A-Z', () => {
    const out = ids({ sort: 'name' });
    assert.deepEqual(out.slice(0, 3), ['!aaaa0002', '!aaaa0005', '!aaaa0008']); // alpha, Late, NoSeen
});

test('sort: distance ascending, nodes without position last, ties by last heard', () => {
    const out = ids({ sort: 'distance' });
    assert.deepEqual(out.slice(0, 3), ['!aaaa0004', '!aaaa0002', '!aaaa0001']); // 100 (newer), 100, 5000
    assert.ok(out.slice(3).every((id) => ctx.distance({ node_id: id }) === null));
});

test('sort: hops ascending, unknown hops last', () => {
    const out = ids({ sort: 'hops' });
    assert.equal(out[0], '!aaaa0001'); // 0 hops
    assert.equal(out[out.length - 1], '!aaaa0007'); // "" -> unknown
});

test('sort: favorites first keeps last-heard order inside each group', () => {
    const out = ids({ sort: 'favorites_first' });
    assert.equal(out[0], '!aaaa0001');
    assert.deepEqual(out.slice(1, 4), ['!aaaa0004', '!aaaa0005', '!aaaa0006']);
});

test('sort does not mutate the input array', () => {
    const copy = NODES.map((n) => n.node_id);
    V.sortNodes(NODES, 'name', ctx);
    assert.deepEqual(NODES.map((n) => n.node_id), copy);
});

test('normalizeNodeFilters: junk and unknown sort fall back to defaults; flags are strict booleans', () => {
    assert.deepEqual({ ...V.normalizeNodeFilters(null) }, { ...V.DEFAULT_NODE_FILTERS });
    const n = V.normalizeNodeFilters({ sort: 'bogus', favoritesOnly: 'true', hideOffline: true, extra: 1 });
    assert.equal(n.sort, 'last_heard');
    assert.equal(n.favoritesOnly, false);
    assert.equal(n.hideOffline, true);
    assert.equal('extra' in n, false);
});

test('activeFilterCount counts filters, not the sort', () => {
    assert.equal(V.activeFilterCount(null), 0);
    assert.equal(V.activeFilterCount({ sort: 'name' }), 0);
    assert.equal(V.activeFilterCount({ favoritesOnly: true, hideOffline: true, knownKeyOnly: true }), 3);
});

test('search-style narrowing composes on top of the filtered list', () => {
    const filtered = V.filterAndSortNodes(NODES, { hideOffline: true }, ctx);
    const searched = filtered.filter((n) => n.clean_name.toLowerCase().includes('r'));
    assert.ok(searched.every((n) => n.node_id !== '!aaaa0003'));
});


// --------------------------------------------- U2: message click -> node card
const el = (matches) => ({ closest: (sel) => (matches.some((m) => sel.split(',').map((x) => x.trim()).includes(m)) ? {} : null) });

test('click filtering: plain bubble text counts, interactive children and selections do not', () => {
    assert.equal(V.isPlainMessageClick(el([]), false), true);
    for (const cls of ['.message-actions-trigger', '.message-reply-quote', '.message-retry-btn', 'a', 'button', 'input']) {
        assert.equal(V.isPlainMessageClick(el([cls]), false), false, cls);
    }
    assert.equal(V.isPlainMessageClick(el([]), true), false, 'text selected');
    assert.equal(V.isPlainMessageClick(null, false), false);
    assert.equal(V.isPlainMessageClick({}, false), false);
});

test('sender resolution: received -> node_id, own -> local node, reply metadata ignored', () => {
    const ctx = { isOwn: (m) => m.kind === 'me', localNodeId: '!B0F14D2A' };
    assert.equal(V.resolveSenderNodeId({ kind: 'rx', node_id: '!1FA065F0' }, ctx), '!1fa065f0');
    assert.equal(V.resolveSenderNodeId({ kind: 'me', node_id: '!1fa065f0' }, ctx), '!b0f14d2a');
    // the quoted original's sender is not the sender of THIS message
    assert.equal(V.resolveSenderNodeId({ kind: 'rx', node_id: '!1fa065f0', reply_to: { node_id: '!99999999' } }, ctx), '!1fa065f0');
    assert.equal(V.resolveSenderNodeId({ kind: 'system', node_id: '!1fa065f0' }, ctx), null);
    assert.equal(V.resolveSenderNodeId({ kind: 'rx', node_id: '' }, ctx), null);
    assert.equal(V.resolveSenderNodeId({ kind: 'rx', node_id: 'channel' }, ctx), null);
    assert.equal(V.resolveSenderNodeId({ kind: 'me' }, { isOwn: () => true, localNodeId: '' }), null);
    assert.equal(V.resolveSenderNodeId(null, ctx), null);
});

test('planSenderFocus: visible -> focus, known but not shown -> hidden_by_filter, else not_found', () => {
    const visible = new Set(['!aaaa0001']);
    const known = new Set(['!aaaa0001', '!aaaa0002']);
    assert.equal(V.planSenderFocus('!aaaa0001', visible, known), 'focus');
    assert.equal(V.planSenderFocus('!aaaa0002', visible, known), 'hidden_by_filter');
    assert.equal(V.planSenderFocus('!aaaa0003', visible, known), 'not_found');
    assert.equal(V.planSenderFocus(null, visible, known), 'not_found');
});

test('hidden-by-filter end to end: the real filters hide an ignored sender; "Show" state reveals it', () => {
    const hidden = V.filterAndSortNodes(NODES, null, ctx).map((n) => n.node_id);
    const known = new Set(NODES.map((n) => n.node_id));
    assert.equal(V.planSenderFocus('!aaaa0003', new Set(hidden), known), 'hidden_by_filter');
    const ignoredNode = NODES.find((n) => n.node_id === '!aaaa0003');
    const reveal = V.stateRevealingNode({ sort: 'name', hideOffline: true, directOnly: true }, ignoredNode);
    assert.equal(reveal.sort, 'name', 'sort is kept');
    assert.equal(reveal.hideOffline, false);
    assert.equal(reveal.ignoredOnly, true);
    const shown = V.filterAndSortNodes(NODES, reveal, ctx).map((n) => n.node_id);
    assert.ok(shown.includes('!aaaa0003'));
    // a normal node just gets every filter dropped
    const plain = V.stateRevealingNode({ hideOffline: true }, NODES[1]);
    assert.equal(V.activeFilterCount(plain), 0);
    assert.ok(V.filterAndSortNodes(NODES, plain, ctx).some((n) => n.node_id === '!aaaa0002'));
});

// ------------------------------------------------ U3: map / node-list interaction
const actionIds = (items) => Array.from(items.map((i) => i.id)); // main-realm array (vm arrays fail deepStrictEqual)
const plain = (value) => JSON.parse(JSON.stringify(value));

test('action menu: full set for a positioned node with an unknown, requestable key', () => {
    const items = V.nodeActionItems({ favorite: false, ignored: false }, { hasPosition: true, keyUnknown: true, canRequestKey: true });
    assert.deepEqual(actionIds(items), ['message', 'favorite', 'ignore', 'waypoint_here', 'center', 'request_key',
        'request_telemetry', 'request_position', 'traceroute', 'set_reference', 'copy_coordinates', 'details']);
});

test('action menu: favorite / ignore carry their current state (drives Unfavorite / Unignore labels)', () => {
    const items = V.nodeActionItems({ favorite: true, ignored: true }, { hasPosition: true });
    assert.equal(items.find((i) => i.id === 'favorite').on, true);
    assert.equal(items.find((i) => i.id === 'ignore').on, true);
    const plain = V.nodeActionItems({}, { hasPosition: true });
    assert.equal(plain.find((i) => i.id === 'favorite').on, false);
    assert.equal(plain.find((i) => i.id === 'ignore').on, false);
});

test('action menu: Request key only while the key is unknown AND a request is allowed', () => {
    const has = (ctx) => actionIds(V.nodeActionItems({}, ctx)).includes('request_key');
    assert.equal(has({ keyUnknown: true, canRequestKey: true }), true);
    assert.equal(has({ keyUnknown: true, canRequestKey: false }), false, 'request already queued / not allowed');
    assert.equal(has({ keyUnknown: false, canRequestKey: true }), false, 'key already known');
    assert.equal(has({}), false);
});

test('action menu: position-dependent items disappear for a node without a position', () => {
    const out = actionIds(V.nodeActionItems({}, { hasPosition: false }));
    for (const id of ['waypoint_here', 'center', 'set_reference', 'copy_coordinates']) assert.ok(!out.includes(id), id);
    for (const id of ['message', 'favorite', 'ignore', 'details', 'request_telemetry', 'request_position', 'traceroute']) assert.ok(out.includes(id), id);
});

test('click vs double click: second click on the same node within the window is a double', () => {
    let r = V.classifyNodeClick(null, '!a', 1000);
    assert.equal(r.kind, 'single');
    r = V.classifyNodeClick(r.next, '!a', 1200);
    assert.equal(r.kind, 'double');
    // a third quick click starts over instead of chaining doubles
    r = V.classifyNodeClick(r.next, '!a', 1300);
    assert.equal(r.kind, 'single');
});

test('click vs double click: other node, or too slow, stays a single click', () => {
    const first = V.classifyNodeClick(null, '!a', 1000).next;
    assert.equal(V.classifyNodeClick(first, '!b', 1100).kind, 'single');
    assert.equal(V.classifyNodeClick(first, '!a', 1000 + 351).kind, 'single');
    assert.equal(V.classifyNodeClick(first, '!a', 1000 + 350).kind, 'double');
    assert.equal(V.classifyNodeClick(first, '!a', 1400, 500).kind, 'double', 'window is configurable');
});

function fakeClock() {
    const timers = new Map(); let nextId = 1;
    return {
        setTimer: (fn, ms) => { const id = nextId++; timers.set(id, { fn, ms }); return id; },
        clearTimer: (id) => timers.delete(id),
        fire: () => { const all = [...timers.values()]; timers.clear(); all.forEach((t) => t.fn()); },
        pending: () => timers.size,
    };
}
function detector(clock, fired) {
    return V.createLongPressDetector({ delayMs: 500, tolerancePx: 10, setTimer: clock.setTimer, clearTimer: clock.clearTimer, onLongPress: (p) => fired.push(p) });
}

test('long-press: a held single finger fires once with its start point', () => {
    const clock = fakeClock(); const fired = []; const d = detector(clock, fired);
    d.start({ x: 40, y: 50 }, 1);
    d.move({ x: 43, y: 52 }, 1); // jitter inside tolerance
    assert.equal(clock.pending(), 1);
    clock.fire();
    assert.deepEqual(plain(fired), [{ x: 40, y: 50 }]);
    assert.equal(d.end(), true, 'the release after a long press must be swallowed');
    assert.equal(d.end(), false);
});

test('long-press: panning beyond the tolerance cancels it', () => {
    const clock = fakeClock(); const fired = []; const d = detector(clock, fired);
    d.start({ x: 0, y: 0 }, 1);
    d.move({ x: 30, y: 0 }, 1);
    assert.equal(clock.pending(), 0);
    clock.fire();
    assert.deepEqual(fired, []);
});

test('long-press: a second finger (pinch) cancels it, and never starts with two fingers', () => {
    const clock = fakeClock(); const fired = []; const d = detector(clock, fired);
    d.start({ x: 0, y: 0 }, 1);
    d.move({ x: 1, y: 1 }, 2);
    assert.equal(clock.pending(), 0);
    d.start({ x: 0, y: 0 }, 2);
    assert.equal(clock.pending(), 0);
    clock.fire();
    assert.deepEqual(fired, []);
});

test('long-press: releasing early is a plain tap (nothing fires, release not swallowed)', () => {
    const clock = fakeClock(); const fired = []; const d = detector(clock, fired);
    d.start({ x: 5, y: 5 }, 1);
    assert.equal(d.end(), false);
    clock.fire();
    assert.deepEqual(fired, []);
    assert.equal(d.isPending(), false);
});

test('chat list follow: scroll when the entry exists but is not visible, wait while unrendered', () => {
    const follow = { id: '!a', at: 1000 };
    assert.equal(V.chatListFollowAction(follow, 1500, true, false), 'scroll');
    assert.equal(V.chatListFollowAction(follow, 1500, true, true), 'done');
    assert.equal(V.chatListFollowAction(follow, 1500, false, false), 'wait', 'entry rendered later');
    assert.equal(V.chatListFollowAction(follow, 1000 + 8001, true, false), 'expire');
    assert.equal(V.chatListFollowAction(null, 1500, true, false), 'none');
});

console.log(`test_chat_views.mjs: ${passed} tests passed`);
