/* static/targets.js
 *
 * The shared navigation target store (PR 4): one controller owning the
 * normalized node+channel target model, the MCA capability overlays, the
 * selected {kind,id} target, subscription/event notification, and
 * generation/epoch protection. Loaded before files.js and chat.js so every
 * workspace — the Nodes/sidebar area, the Files contact list + Send-dialog
 * recipients, the Files counterparty filter, and future desktop/mobile
 * redesigns — reads the SAME model instead of each re-merging the raw
 * endpoints.
 *
 * A "target" has exactly two kinds: `node` (a canonical `!xxxxxxxx` transport
 * address) and `channel` (a radio channel id). An "MCA Contact" is NOT a
 * third kind — it is the security/capability state attached to a *node*
 * target. A node from `/api/nodes_management` and an MCA binding from
 * `/api/mca/contacts` with the same canonical address are one target; an
 * MCA-only contact (missing from the node list) stays available as a node
 * target (its canonical id is the fallback display name). Nodes are NEVER
 * matched/deduped by display name — identity is `{kind, id}` alone.
 *
 * Sources merged (each keeps its own last-known-good slice + degraded flag,
 * so a transient failure never silently replaces a valid slice with empty):
 *   GET /api/nodes_management    -> node list (name/ignored/favorite/last_seen)
 *   GET /api/mca/contacts        -> MCA trust bindings (status/fingerprint/…)
 *   GET /api/mca/key-requests    -> worker-published key-request capability
 *   GET /api/base_status         -> local node identity
 *   GET /api/chats               -> CACHED channel projection (no
 *                                   `refresh_channels` — this store never
 *                                   forces radio channel discovery)
 *
 * The capability matrix (can_send_file / can_request_key /
 * file_unavailable_reason) is computed in ONE place — computeCapability() —
 * and is never re-derived in a UI component. A node must never become
 * sendable merely because it exists in `/api/nodes_management`: only a
 * trusted worker-published MCA binding (`trust_state === "ready"`) produces
 * `can_send_file === true`. The worker's rate-limit window and workspace
 * quota stay authoritative for key requests; this store only reflects the
 * worker-published `key_request_state`/`can_request_key` snapshot.
 *
 * This is a self-contained module (no build step): it defines its own
 * minimal fetch helper and never imports files.js/chat.js internals.
 */

'use strict';

(function () {
    // ---- constants ---------------------------------------------------------

    var CONTACT_ID_RE = /^![0-9a-f]{8}$/;  // mirrors _CONTACT_ID_RE server-side

    // The public ContactStatus strings from meshsrv/attachments/contacts.py,
    // mapped to the PR 4 capability-model `trust_state` vocabulary.
    var TRUST_STATE_BY_STATUS = {
        trusted: 'ready',
        confirmation_required: 'confirmation_required',
        key_changed: 'changed',
        key_unknown: 'unknown',
    };

    var KEY_REQUEST_STATES = {
        idle: 'idle',
        queued: 'queued',
        waiting_response: 'waiting_response',
        retry_available: 'retry_available',
    };

    // ---- single internal state object --------------------------------------

    var state = {
        generation: 0,          // bumped on every successful merge — consumers guard on it
        refreshEpoch: 0,        // bumped at the start of each refresh(); stale responses dropped
        loading: {},            // sourceKey -> in-flight Promise (joinable)
        degraded: {},           // sourceKey -> true while serving last-known-good
        lastGood: {
            nodes: null,        // { nodes: [], total: 0 }
            contacts: null,     // { contacts: [] }
            keyRequests: null,  // { key_requests: [] }
            baseStatus: null,   // { node_id, node_name }
            channels: null,     // { channels: [] }
        },
        localNodeId: '',
        localNodeName: '',
        nodeTargets: [],        // normalized node targets (includes the local node)
        byNodeId: {},
        channelTargets: [],
        byChannelId: {},
        selection: null,        // { kind: 'node'|'channel', id } or null
        subscribers: [],
    };

    // ---- small helpers -----------------------------------------------------

    function canonicalId(id) {
        return String(id || '').toLowerCase();
    }

    function trustStateFromStatus(status) {
        return TRUST_STATE_BY_STATUS[status] || 'unknown';
    }

    // The one centralized capability matrix. Inputs are the target's identity
    // facts; outputs are the three public capability fields every consumer
    // reads. `backendCanRequestKey` is the worker-published authoritative
    // boolean for addresses that HAVE a key-request entry (undefined when
    // idle/absent), folded in only for the retry_available case.
    function computeCapability(opts) {
        opts = opts || {};
        var kind = opts.kind;
        var isLocal = Boolean(opts.is_local);
        var trustState = opts.trust_state;
        var keyRequestState = opts.key_request_state;
        var backendCanRequestKey = opts.backend_can_request_key;

        if (isLocal) {
            return { can_send_file: false, can_request_key: false, file_unavailable_reason: 'local_node' };
        }
        if (kind === 'channel') {
            return { can_send_file: false, can_request_key: false, file_unavailable_reason: 'channel_not_supported' };
        }
        switch (trustState) {
            case 'ready':
                return { can_send_file: true, can_request_key: false, file_unavailable_reason: null };
            case 'confirmation_required':
                // action = confirm key (a KEY_UNVERIFIED binding awaiting trust)
                return { can_send_file: false, can_request_key: false, file_unavailable_reason: 'key_unverified' };
            case 'changed':
                // action = accept/reject the pending key rotation
                return { can_send_file: false, can_request_key: false, file_unavailable_reason: 'key_changed' };
            case 'unknown':
            default:
                // queued / waiting_response = a request is already in flight -> disabled.
                if (keyRequestState === 'queued' || keyRequestState === 'waiting_response') {
                    return { can_send_file: false, can_request_key: false, file_unavailable_reason: 'key_unknown' };
                }
                // idle (never requested) or retry_available (rate-limit window
                // elapsed). For retry_available the worker's authoritative
                // `can_request_key` is the final word; idle always permits.
                var canRequest = keyRequestState === 'retry_available'
                    ? backendCanRequestKey !== false
                    : true;
                return { can_send_file: false, can_request_key: canRequest, file_unavailable_reason: 'key_unknown' };
        }
    }

    // ---- minimal fetch helper (self-contained) -----------------------------

    function api(url) {
        var resp;
        return (typeof fetch === 'function' ? fetch(url, { headers: { 'Cache-Control': 'no-cache' } }) : Promise.reject(new Error('no fetch'))).then(
            function (r) { resp = r; return r.json().catch(function () { return null; }); },
            function () { return null; }
        ).then(function (data) {
            return { status: resp ? resp.status : 0, data: data || null };
        });
    }

    // ---- last-known-good per-source load -----------------------------------

    function sourceKey(name) {
        return 'targets:' + name;
    }

    // Load one source slice with its own in-flight guard. On a transient
    // failure the previous good slice is preserved (degraded), never replaced
    // with empty. Returns the in-flight (joinable) promise for that source.
    function loadSource(name, url, apply) {
        var key = sourceKey(name);
        if (state.loading[key]) return state.loading[key];
        state.loading[key] = api(url).then(function (r) {
            delete state.loading[key];
            if (r.status === 200 && r.data) {
                state.lastGood[name] = r.data;
                delete state.degraded[name];
            } else {
                // Non-200 / empty: preserve last-known-good and mark degraded.
                // A 503 "not ready" is treated the same — it must not wipe a
                // previously valid slice.
                state.degraded[name] = true;
            }
            if (apply) apply();
            return r;
        }, function () {
            delete state.loading[key];
            state.degraded[name] = true;
            if (apply) apply();
            return { status: 0, data: null };
        });
        return state.loading[key];
    }

    // ---- merge -------------------------------------------------------------

    function keyRequestMap() {
        var raw = state.lastGood.keyRequests;
        var map = {};
        if (!raw || !Array.isArray(raw.key_requests)) return map;
        raw.key_requests.forEach(function (kr) {
            var id = canonicalId(kr.contact_id);
            if (!id) return;
            map[id] = {
                key_request_state: kr.key_request_state || 'idle',
                can_request_key: kr.can_request_key === true,
            };
        });
        return map;
    }

    function mergeTargets() {
        var nodesRaw = state.lastGood.nodes;
        var contactsRaw = state.lastGood.contacts;
        var krMap = keyRequestMap();
        var nodes = (nodesRaw && Array.isArray(nodesRaw.nodes)) ? nodesRaw.nodes : [];
        var bindings = (contactsRaw && Array.isArray(contactsRaw.contacts)) ? contactsRaw.contacts : [];

        var byNodeId = {};
        var order = [];

        // Pass 1: MCA bindings seed a node target (source = mca_only until a
        // matching node is seen). Bindings carry no display name — the node
        // slice (or the canonical id fallback) supplies it.
        bindings.forEach(function (b) {
            var id = canonicalId(b.contact_id);
            if (!id) return;
            byNodeId[id] = {
                kind: 'node',
                id: id,
                display_name: '',
                source: 'mca_only',
                is_local: false,
                trust_state: trustStateFromStatus(b.status),
                contact_status: b.status || 'key_unknown',
                fingerprint: b.fingerprint || '',
                pending_fingerprint: b.pending_fingerprint || '',
                key_epoch: b.key_epoch,
                pending_key_epoch: b.pending_key_epoch,
                hasBinding: true,
                ignored: false,
                favorite: false,
                last_seen: null,
                // capability fields filled in pass 3
                key_request_state: 'idle',
                can_send_file: false,
                can_request_key: false,
                file_unavailable_reason: null,
            };
            if (order.indexOf(id) === -1) order.push(id);
        });

        // Pass 2: nodes overlay names + metadata and mark the local node.
        nodes.forEach(function (n) {
            var id = canonicalId(n.node_id);
            if (!id) return;
            if (!byNodeId[id]) {
                byNodeId[id] = {
                    kind: 'node',
                    id: id,
                    display_name: '',
                    source: 'node_only',
                    is_local: false,
                    trust_state: 'unknown',
                    contact_status: 'key_unknown',
                    fingerprint: '',
                    pending_fingerprint: '',
                    key_epoch: null,
                    pending_key_epoch: null,
                    hasBinding: false,
                    ignored: false,
                    favorite: false,
                    last_seen: null,
                    key_request_state: 'idle',
                    can_send_file: false,
                    can_request_key: false,
                    file_unavailable_reason: null,
                };
                order.push(id);
            }
            var t = byNodeId[id];
            if (n.name) t.display_name = n.name;
            if (t.source === 'mca_only') t.source = 'node_and_mca';
            t.ignored = Boolean(n.ignored);
            t.favorite = Boolean(n.favorite);
            t.last_seen = n.last_seen;
        });

        // Pass 2b: ensure the local node is present even when the node list
        // does not (yet) include it — its identity comes from base_status.
        var localId = canonicalId(state.localNodeId);
        if (localId && !byNodeId[localId]) {
            byNodeId[localId] = {
                kind: 'node',
                id: localId,
                display_name: state.localNodeName || '',
                source: 'node_only',
                is_local: false,   // set to true below
                trust_state: 'unknown',
                contact_status: 'key_unknown',
                fingerprint: '',
                pending_fingerprint: '',
                key_epoch: null,
                pending_key_epoch: null,
                hasBinding: false,
                ignored: false,
                favorite: false,
                last_seen: null,
                key_request_state: 'idle',
                can_send_file: false,
                can_request_key: false,
                file_unavailable_reason: null,
            };
            order.push(localId);
        }

        // Pass 3: local flag + key-request state + central capability matrix.
        order.forEach(function (id) {
            var t = byNodeId[id];
            t.is_local = Boolean(localId && id === localId);
            var kr = krMap[id];
            t.key_request_state = kr ? kr.key_request_state : 'idle';
            var cap = computeCapability({
                kind: 'node',
                is_local: t.is_local,
                trust_state: t.trust_state,
                key_request_state: t.key_request_state,
                backend_can_request_key: kr ? kr.can_request_key : undefined,
            });
            t.can_send_file = cap.can_send_file;
            t.can_request_key = cap.can_request_key;
            t.file_unavailable_reason = cap.file_unavailable_reason;
        });

        var nodeTargets = order.map(function (id) { return byNodeId[id]; });
        nodeTargets.sort(function (a, b) {
            var an = (a.display_name || '').toLowerCase();
            var bn = (b.display_name || '').toLowerCase();
            if (an < bn) return -1;
            if (an > bn) return 1;
            return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
        });

        // Channels: navigation-only targets, never file recipients.
        var channelsRaw = state.lastGood.channels;
        var channels = (channelsRaw && Array.isArray(channelsRaw.channels)) ? channelsRaw.channels : [];
        var byChannelId = {};
        var channelTargets = channels.map(function (c) {
            var id = canonicalId(c.id);
            var cap = computeCapability({ kind: 'channel' });
            var t = {
                kind: 'channel',
                id: id,
                display_name: c.name || id,
                source: 'channel',
                is_local: false,
                trust_state: null,
                contact_status: null,
                can_send_file: cap.can_send_file,
                can_request_key: cap.can_request_key,
                key_request_state: null,
                file_unavailable_reason: cap.file_unavailable_reason,
                index: c.index,
            };
            byChannelId[id] = t;
            return t;
        });
        channelTargets.sort(function (a, b) {
            if (a.index !== b.index) return (a.index < b.index ? -1 : 1);
            return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
        });

        state.byNodeId = byNodeId;
        state.nodeTargets = nodeTargets;
        state.byChannelId = byChannelId;
        state.channelTargets = channelTargets;
    }

    function notify() {
        var subs = state.subscribers.slice();
        var selection = state.selection;
        subs.forEach(function (fn) {
            try { fn({ generation: state.generation, selection: selection }); } catch (_) { /* subscriber must not break others */ }
        });
    }

    // ---- public surface ----------------------------------------------------

    function refresh() {
        state.refreshEpoch++;
        // The local node identity is read from base_status, applied inline
        // during the load so `mergeTargets` sees it; all other slices merge
        // once every source settles.
        return Promise.all([
            loadSource('nodes', '/api/nodes_management'),
            loadSource('contacts', '/api/mca/contacts'),
            loadSource('keyRequests', '/api/mca/key-requests'),
            loadSource('channels', '/api/chats'),
            loadSource('baseStatus', '/api/base_status', function () {
                var bs = state.lastGood.baseStatus;
                if (bs) state.localNodeId = canonicalId(bs.node_id || '');
                if (bs) state.localNodeName = bs.node_name || '';
            }),
        ]).then(function () {
            mergeTargets();
            state.generation++;
            notify();
            return state.generation;
        });
    }

    function nodeTargets() { return state.nodeTargets; }
    function channelTargets() { return state.channelTargets; }
    function nodeTotal() {
        // The authoritative node count from /api/nodes_management (which the
        // node-manager "total" badge reflects); falls back to the merged
        // target count when that slice has never loaded.
        var raw = state.lastGood.nodes;
        if (raw && typeof raw.total === 'number') return raw.total;
        return state.nodeTargets.length;
    }
    function allTargets() { return state.nodeTargets.concat(state.channelTargets); }

    function getNode(id) {
        return state.byNodeId[canonicalId(id)] || null;
    }
    function getChannel(id) {
        return state.byChannelId[canonicalId(id)] || null;
    }
    function getTarget(kind, id) {
        if (kind === 'channel') return getChannel(id);
        return getNode(id);
    }

    function select(kind, id) {
        state.selection = kind ? { kind: kind, id: canonicalId(id) } : null;
        notify();
        return state.selection;
    }
    function clearSelection() {
        state.selection = null;
        notify();
    }
    function selected() {
        return state.selection;
    }

    function subscribe(fn) {
        if (typeof fn !== 'function') return function () {};
        state.subscribers.push(fn);
        return function () {
            var i = state.subscribers.indexOf(fn);
            if (i !== -1) state.subscribers.splice(i, 1);
        };
    }

    function localNodeId() { return state.localNodeId; }
    function generation() { return state.generation; }
    function isDegraded(name) { return Boolean(state.degraded[name]); }

    window.MeshCenterTargets = {
        refresh: refresh,
        nodeTargets: nodeTargets,
        channelTargets: channelTargets,
        nodeTotal: nodeTotal,
        allTargets: allTargets,
        getNode: getNode,
        getChannel: getChannel,
        getTarget: getTarget,
        select: select,
        clearSelection: clearSelection,
        selected: selected,
        subscribe: subscribe,
        localNodeId: localNodeId,
        generation: generation,
        isDegraded: isDegraded,
        // Exported for the .mjs tests (and any future consumer that wants the
        // matrix without touching the store): pure, no hidden state.
        computeCapability: computeCapability,
        trustStateFromStatus: trustStateFromStatus,
        canonicalId: canonicalId,
    };
})();
