// tests/frontend/test_targets_ui.mjs
//
// Dependency-free behavior tests for static/targets.js (PR 4 shared target
// model). This repo has no build step and no JS test framework (CI's JS gate
// is `node --check` — syntax only), so, mirroring test_files_ui.mjs, this
// uses Node's built-in `vm` to execute the REAL targets.js source against a
// minimal fetch/global surface, and Node's built-in `assert`.
// Runnable as `node tests/frontend/test_targets_ui.mjs`.
//
// Covers the PR 4 frontend-store requirements:
//   merge/dedup — a node from /api/nodes_management and an MCA binding with
//       the same canonical `!xxxxxxxx` address become ONE target; never
//       dedupe by display name; an MCA-only contact stays available as a node
//       target; a node without a binding is `trust_state === "unknown"`.
//   selection  — select/clearSelection/selected + canonical-id casing; the
//       subscriber notification (generation + selection) on refresh/select.
//   channel    — channel targets are navigation-only (never sendable, never
//       key-requestable, reason `channel_not_supported`).
//   races      — the per-source in-flight guard (a concurrent refresh joins,
//       never double-fetches) and the last-known-good/degraded preservation
//       (a transient failure never wipes a valid slice).

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const scriptPath = path.join(__dirname, '..', '..', 'static', 'targets.js');
const source = readFileSync(scriptPath, 'utf8');

// ---- minimal fetch/global surface ------------------------------------------

function json(body) {
    return { status: 200, json: async () => body };
}
function raw(status, body) {
    return { status, json: async () => body };
}

function baseStatus(node_id = '!11111111', node_name = 'Me') {
    return { node_id, node_name, profile_id: 'p1' };
}
function nodesBody(nodes, total) {
    return { nodes, total: total === undefined ? nodes.length : total };
}
function contactsBody(contacts) {
    return { ok: true, contacts };
}
function keyRequestsBody(keyRequests) {
    return { ok: true, key_requests: keyRequests };
}
function channelsBody(channels) {
    return { chats: [], channels, total_unread: 0 };
}

function defaultHandler(overrides = {}) {
    return async (url) => {
        if (Object.prototype.hasOwnProperty.call(overrides, url)) return overrides[url];
        switch (url) {
            case '/api/nodes_management': return json(nodesBody([]));
            case '/api/mca/contacts': return json(contactsBody([]));
            case '/api/mca/key-requests': return json(keyRequestsBody([]));
            case '/api/chats': return json(channelsBody([]));
            case '/api/base_status': return json(baseStatus());
            default: throw new Error('unexpected fetch: ' + url);
        }
    };
}

function makeSandbox(fetchImpl) {
    const calls = [];
    const sandbox = {
        console,
        Promise,
        window: {},
        fetch: async (url, options) => {
            calls.push(url);
            return fetchImpl(url, options);
        },
    };
    vm.createContext(sandbox);
    vm.runInContext(source, sandbox, { filename: 'targets.js' });
    return { store: sandbox.window.MeshCenterTargets, calls };
}

// ---- merge / dedup ----------------------------------------------------------

// The store always synthesizes the local node (from base_status) even when the
// node list omits it, so merge/dedup assertions count non-local targets only.
function nonLocal(store) {
    return store.nodeTargets().filter((t) => !t.is_local);
}

async function testMergeNodeAndBindingBecomeOneTarget() {
    const { store } = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([
            { node_id: '!ABCDEF12', name: 'Alice', ignored: false, favorite: true, last_seen: 123 },
        ])),
        '/api/mca/contacts': json(contactsBody([
            { contact_id: '!abcdef12', status: 'trusted', fingerprint: 'fp', key_epoch: 1 },
        ])),
    }));
    await store.refresh();

    assert.equal(nonLocal(store).length, 1, 'one target, not two');
    const t = store.getNode('!abcdef12');
    assert.ok(t, 'target present');
    assert.equal(t.kind, 'node');
    assert.equal(t.source, 'node_and_mca');
    assert.equal(t.display_name, 'Alice', 'name comes from the node slice');
    assert.equal(t.trust_state, 'ready', 'trusted binding maps to ready');
    assert.equal(t.can_send_file, true, 'ready -> sendable');
    assert.equal(t.hasBinding, true);
}

async function testCanonicalAddressIsCaseInsensitive() {
    const { store } = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([
            { node_id: '!ABCDEF12', name: 'Alice' },
        ])),
        '/api/mca/contacts': json(contactsBody([
            { contact_id: '!ABCDEF12', status: 'trusted' },
        ])),
    }));
    await store.refresh();
    assert.equal(nonLocal(store).length, 1, 'same address, different case -> still one target');
    assert.equal(store.getNode('!abcdef12').source, 'node_and_mca');
}

async function testNeverDedupeByDisplayName() {
    const { store } = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([
            { node_id: '!11111111', name: 'Alice' },
            { node_id: '!22222222', name: 'Alice' },
        ])),
    }));
    await store.refresh();
    assert.equal(store.nodeTargets().length, 2, 'same name, different address -> two targets');
    assert.ok(store.getNode('!11111111'));
    assert.ok(store.getNode('!22222222'));
}

async function testMcaOnlyContactStaysAvailableAsNodeTarget() {
    const { store } = makeSandbox(defaultHandler({
        '/api/mca/contacts': json(contactsBody([
            { contact_id: '!abcdef12', status: 'key_unknown' },
        ])),
    }));
    await store.refresh();
    assert.equal(nonLocal(store).length, 1);
    const t = store.getNode('!abcdef12');
    assert.equal(t.kind, 'node');
    assert.equal(t.source, 'mca_only');
    assert.equal(t.display_name, '', 'no node slice -> name left for the id fallback');
    assert.equal(t.trust_state, 'unknown');
    assert.equal(t.can_send_file, false);
}

async function testNodeWithoutBindingIsUnknownAndRequestable() {
    const { store } = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([
            { node_id: '!33333333', name: 'Bob' },
        ])),
    }));
    await store.refresh();
    const t = store.getNode('!33333333');
    assert.equal(t.source, 'node_only');
    assert.equal(t.trust_state, 'unknown');
    assert.equal(t.can_send_file, false, 'mere existence in nodes_management must never make a node sendable');
    assert.equal(t.can_request_key, true, 'idle unknown node may request a key');
    assert.equal(t.file_unavailable_reason, 'key_unknown');
}

async function testKeyRequestOverlayDrivesRequestCapability() {
    const { store } = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([{ node_id: '!33333333', name: 'Bob' }])),
        '/api/mca/key-requests': json(keyRequestsBody([
            { contact_id: '!33333333', key_request_state: 'queued', can_request_key: false },
        ])),
    }));
    await store.refresh();
    const t = store.getNode('!33333333');
    assert.equal(t.key_request_state, 'queued');
    assert.equal(t.can_request_key, false, 'queued -> disabled');

    // retry_available with the backend's authoritative can_request_key=true
    const sandbox2 = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([{ node_id: '!44444444', name: 'Cid' }])),
        '/api/mca/key-requests': json(keyRequestsBody([
            { contact_id: '!44444444', key_request_state: 'retry_available', can_request_key: true },
        ])),
    }));
    await sandbox2.store.refresh();
    const t2 = sandbox2.store.getNode('!44444444');
    assert.equal(t2.key_request_state, 'retry_available');
    assert.equal(t2.can_request_key, true);
}

// ---- selection --------------------------------------------------------------

async function testSelectClearAndSelected() {
    const { store } = makeSandbox(defaultHandler());
    assert.equal(store.selected(), null);

    // Objects cross the vm realm, so compare fields (deepEqual would also
    // compare the vm's Object.prototype against the host's).
    const sel = store.select('node', '!ABCDEF12');
    assert.equal(sel.kind, 'node');
    assert.equal(sel.id, '!abcdef12', 'id is canonicalized');
    const current = store.selected();
    assert.equal(current.kind, 'node');
    assert.equal(current.id, '!abcdef12');

    store.select('channel', 'LongFast');
    assert.equal(store.selected().kind, 'channel');
    assert.equal(store.selected().id, 'longfast');

    store.clearSelection();
    assert.equal(store.selected(), null);
}

async function testSelectNullKindClears() {
    const { store } = makeSandbox(defaultHandler());
    store.select('node', '!11111111');
    store.select(null, '!11111111');
    assert.equal(store.selected(), null);
}

async function testSubscribersNotifiedOnRefreshAndSelect() {
    const { store } = makeSandbox(defaultHandler());
    const events = [];
    store.subscribe((e) => events.push(e));

    await store.refresh();
    assert.equal(events.length, 1);
    assert.equal(events[0].generation, 1);
    assert.equal(events[0].selection, null);

    store.select('node', '!11111111');
    assert.equal(events.length, 2);
    assert.equal(events[1].selection.id, '!11111111');

    // Unsubscribe: no further events.
    const unsub = store.subscribe(() => { throw new Error('must not fire'); });
    unsub();
    store.select('node', '!22222222');
    assert.equal(events.length, 3, 'unsubscribed callback must not fire');
}

// ---- channel ----------------------------------------------------------------

async function testChannelIsNavigationOnly() {
    const { store } = makeSandbox(defaultHandler({
        '/api/chats': json(channelsBody([
            { id: 'LongFast', name: 'LongFast', index: 0 },
        ])),
    }));
    await store.refresh();
    assert.equal(store.channelTargets().length, 1);
    const c = store.getChannel('LongFast');
    assert.equal(c.kind, 'channel');
    assert.equal(c.source, 'channel');
    assert.equal(c.can_send_file, false);
    assert.equal(c.can_request_key, false);
    assert.equal(c.file_unavailable_reason, 'channel_not_supported');
    assert.equal(c.trust_state, null);
    assert.equal(c.key_request_state, null);
    // Channels never appear among node targets; the only node target here is
    // the synthesized local node (from base_status), which is a node, not a
    // channel.
    assert.ok(store.nodeTargets().every((t) => t.kind === 'node'));
    assert.equal(store.channelTargets().length, 1);
}

// ---- local node -------------------------------------------------------------

async function testLocalNodeIsSelfUnavailable() {
    const { store } = makeSandbox(defaultHandler({
        '/api/base_status': json(baseStatus('!11111111', 'Me')),
        '/api/nodes_management': json(nodesBody([
            { node_id: '!11111111', name: 'Me' },
            { node_id: '!22222222', name: 'You' },
        ])),
    }));
    await store.refresh();
    assert.equal(store.localNodeId(), '!11111111');
    const me = store.getNode('!11111111');
    assert.equal(me.is_local, true);
    assert.equal(me.can_send_file, false);
    assert.equal(me.file_unavailable_reason, 'local_node');
    const you = store.getNode('!22222222');
    assert.equal(you.is_local, false);
}

async function testNodeTotalPrefersRawTotalOverTargetCount() {
    const { store } = makeSandbox(defaultHandler({
        '/api/nodes_management': json(nodesBody([{ node_id: '!11111111', name: 'Me' }], 42)),
    }));
    await store.refresh();
    assert.equal(store.nodeTotal(), 42, 'badge reflects /api/nodes_management total');
}

// ---- races / degradation ----------------------------------------------------

async function testConcurrentRefreshJoinsInFlightAndNeverDoubleFetches() {
    const { store, calls } = makeSandbox(defaultHandler());
    const p1 = store.refresh();
    const p2 = store.refresh();
    await Promise.all([p1, p2]);
    // The race guarantee is the fetch dedup: the second refresh joins the
    // first's in-flight per-source promises rather than issuing its own.
    assert.equal(calls.length, 5, 'five sources fetched once each, not ten');
}

async function testGenerationIncrementsPerMerge() {
    const { store } = makeSandbox(defaultHandler());
    assert.equal(store.generation(), 0);
    await store.refresh();
    assert.equal(store.generation(), 1);
    await store.refresh();
    assert.equal(store.generation(), 2);
}

async function testTransientFailurePreservesLastKnownGoodAndMarksDegraded() {
    let failContacts = false;
    const handler = async (url) => {
        if (url === '/api/mca/contacts') {
            if (failContacts) return raw(503, { ok: false, error_code: 'mca_not_ready' });
            return json(contactsBody([{ contact_id: '!abcdef12', status: 'trusted' }]));
        }
        return defaultHandler()[url] ?? (await defaultHandler()(url));
    };
    const { store } = makeSandbox(handler);
    await store.refresh();
    assert.equal(store.getNode('!abcdef12').trust_state, 'ready');
    assert.equal(store.isDegraded('contacts'), false);

    failContacts = true;
    await store.refresh();
    assert.equal(store.isDegraded('contacts'), true, '503 marks the slice degraded');
    const t = store.getNode('!abcdef12');
    assert.ok(t, 'last-known-good contact preserved');
    assert.equal(t.trust_state, 'ready', 'a 503 must not wipe the valid slice');
}

async function testNetworkErrorPreservesLastKnownGood() {
    let failContacts = false;
    const { store } = makeSandbox(async (url) => {
        if (url === '/api/mca/contacts' && failContacts) {
            throw new Error('network down');
        }
        if (url === '/api/mca/contacts') {
            return json(contactsBody([{ contact_id: '!abcdef12', status: 'key_unknown' }]));
        }
        return defaultHandler()(url);
    });
    await store.refresh();
    assert.ok(store.getNode('!abcdef12'));

    failContacts = true;
    await store.refresh();
    assert.equal(store.isDegraded('contacts'), true);
    assert.ok(store.getNode('!abcdef12'), 'network error keeps last-known-good');
}

// ---- runner ------------------------------------------------------------------

const tests = [
    ['merge: node + binding become one target', testMergeNodeAndBindingBecomeOneTarget],
    ['merge: canonical address is case-insensitive', testCanonicalAddressIsCaseInsensitive],
    ['merge: never dedupe by display name', testNeverDedupeByDisplayName],
    ['merge: mca-only contact stays a node target', testMcaOnlyContactStaysAvailableAsNodeTarget],
    ['merge: node without binding is unknown + requestable', testNodeWithoutBindingIsUnknownAndRequestable],
    ['merge: key-request overlay drives request capability', testKeyRequestOverlayDrivesRequestCapability],
    ['selection: select/clear/selected canonicalizes', testSelectClearAndSelected],
    ['selection: null kind clears', testSelectNullKindClears],
    ['selection: subscribers notified on refresh/select', testSubscribersNotifiedOnRefreshAndSelect],
    ['channel: navigation-only', testChannelIsNavigationOnly],
    ['local node: self unavailable', testLocalNodeIsSelfUnavailable],
    ['local node: total prefers raw total', testNodeTotalPrefersRawTotalOverTargetCount],
    ['race: concurrent refresh joins, no double fetch', testConcurrentRefreshJoinsInFlightAndNeverDoubleFetches],
    ['race: generation increments per merge', testGenerationIncrementsPerMerge],
    ['race: transient 503 preserves last-known-good', testTransientFailurePreservesLastKnownGoodAndMarksDegraded],
    ['race: network error preserves last-known-good', testNetworkErrorPreservesLastKnownGood],
];

let failed = 0;
for (const [name, fn] of tests) {
    try {
        await fn();
        console.log(`ok - ${name}`);
    } catch (err) {
        failed += 1;
        console.error(`not ok - ${name}`);
        console.error(err && err.stack ? err.stack : err);
    }
}

console.log(`\n${tests.length - failed}/${tests.length} passed`);
if (failed > 0) process.exit(1);
