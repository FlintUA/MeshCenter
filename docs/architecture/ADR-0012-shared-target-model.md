# ADR-0012: Shared navigation target model (node / channel / MCA-contact capability)

**Status:** Accepted
**Date:** 2026-09-12
**Context documents:** ADR-0001 (protocol), ADR-0008 (attachments backend layer — the worker-published snapshot model this ADR extends to key-request state), ADR-0010/ADR-0011 (the DIRECT-only route shape the `node` target pins), and the MCAttach Files workspace (PR 1–3).

## Context

Before this ADR, four UI surfaces independently read and re-merged the same three backend slices to answer the same question — "what can I navigate to, and what can I do with it?":

- the **Nodes / sidebar** area (`/api/nodes_management` + `/api/chats` channels),
- the **Files contact list** (`/api/mca/contacts`, plus its own local node filtering),
- the **Files transfer counterparty filter** (a canonical contact id matched against `/api/nodes_management`),
- the **MCAttach Send dialog** recipient list (`/api/mca/contacts` again, cross-referenced against node names).

Each surface carried its own dedup, its own local-node exclusion, its own "can this contact receive a file / request a key" logic, and its own case/name handling. That is a drift hazard: a node can be matched by *display name* in one place and by canonical address in another, so the same radio appears as one target, zero targets, or two targets depending on which workspace the user happens to be looking at. It also duplicated the single most safety-sensitive decision in the product — *when is a node sendable?* — across four code paths, any one of which could regress the "a node must never become sendable merely because it exists in `/api/nodes_management`" invariant.

This ADR collapses those four merges into one shared model, owned by one controller, so the identity and capability rules are written once and every surface reads the same answer.

## Decision

### 1. Exactly two navigation target kinds: `node` and `channel`

An "MCA Contact" is **not** a third target kind. It is the *security/capability state attached to a node target*. A node from `/api/nodes_management` and an MCA binding from `/api/mca/contacts` that share the same canonical address are **one** node target; a contact that appears in the MCA binding list but not (yet) in the node list remains available as a node target (its canonical id is the fallback display name). A node with no binding is still a node target, with `trust_state = "unknown"`.

### 2. Identity is `{kind, id}` — never a display name

A node target's identity is its canonical transport address `!` + 8 lowercase hex (the same `_CONTACT_ID_RE` shape the backend validates at its inbound boundary). A channel target's identity is its channel id. Matching, dedup, and selection are all keyed on `{kind, id}` alone. Display names are presentation-only and are **never** used to match or dedupe — two distinct nodes that happen to share a name remain two targets.

The canonical target model is:

```
{ kind: "node" | "channel",
  id, display_name,
  source: "node_and_mca" | "node_only" | "mca_only" | "channel",
  is_local,
  trust_state: "ready" | "unknown" | "confirmation_required" | "changed" | null,
  can_send_file, can_request_key,
  key_request_state: "idle" | "queued" | "waiting_response" | "retry_available" | null,
  file_unavailable_reason: "local_node" | "channel_not_supported" | "key_unknown" | "key_unverified" | "key_changed" | null }
```

`trust_state` is the canonical vocabulary derived from the public `ContactStatus` strings (`trusted → ready`, `confirmation_required → confirmation_required`, `key_changed → changed`, `key_unknown → unknown`); channels and the local node carry `null`.

### 3. One centralized capability matrix

`can_send_file` / `can_request_key` / `file_unavailable_reason` are computed in exactly one place (the frontend store's `computeCapability()`, mirrored by the backend key-request projection's own `can_request_key`), from the target's `kind`, `is_local`, `trust_state`, and `key_request_state`:

| trust_state | key_request_state | can_send_file | can_request_key | file_unavailable_reason |
|---|---|---|---|---|
| ready | — | true | false | — |
| unknown | idle | false | true | key_unknown |
| unknown | queued | false | false | key_unknown |
| unknown | waiting_response | false | false | key_unknown |
| unknown | retry_available | false | (worker's can_request_key) | key_unknown |
| confirmation_required | — | false | false | key_unverified |
| changed | — | false | false | key_changed |
| (local node) | — | false | false | local_node |
| (channel) | — | false | false | channel_not_supported |

The central invariant, enforced by construction: **a node must never become sendable merely because it exists in `/api/nodes_management`.** Only a trusted, worker-published MCA binding (`trust_state === "ready"`) produces `can_send_file === true`. The `confirmation_required` row is the "confirm key" action; the `changed` row is "accept/reject"; the worker's rate-limit window and workspace quota remain authoritative for whether `retry_available` actually permits a fresh request.

### 4. Channels are navigation-only

A channel target is never a file recipient, never key-requestable, and never expanded into its member node list. Its capability is fixed: `can_send_file=false`, `can_request_key=false`, `trust_state=null`, `key_request_state=null`, `file_unavailable_reason="channel_not_supported"`. This ADR does **not** send an MCAttach OFFER to a channel, convert a channel to broadcast, reuse a channel PSK, change `supports_channel`, or add group encryption/group receipts. Attachment create stays DIRECT node-to-node: `recipient.source_address` is a canonical `!xxxxxxxx` node id, route `DIRECT`, `route_id` the same canonical node id, and the worker remains the final authority (ADR-0001/ADR-0008).

### 5. Backend key-request capability: a worker-published, no-secret snapshot

The "request key" capability is not a single row in one table — it is two facts only the worker can read safely: a `contact_request_key` command still in the bounded command queue (queued), and the persisted `last_request_sent_at` timestamp (sent). `KeyRequestStatePublisher` (`meshsrv/attachments/key_request_snapshot.py`) folds those into one non-secret `KeyRequestState` per address plus a derived `can_request_key`, and publishes the whole mapping as an immutable, atomically-swapped snapshot — the same `RecipientSnapshotPublisher`/`ConnectivityMonitor` shape as ADR-0008. A request thread (and `GET /api/mca/key-requests`) reads only `snapshot()`, never `conn`, the filesystem, the network, or the tick lock.

The published snapshot carries only the enum's public string values and a boolean. It never projects `last_request_sent_at`, any retry/throttle timestamp, the rate-limit window, a raw public identity, X25519 material, a signing key, a private-key path, a DB row, or exception text. `retry_available` vs `waiting_response` is the *only* place a timestamp is consulted, and it is consumed entirely to pick a state string. This is what lets the UI recover `waiting_response`/`retry_available` after a reload — the state is derived from backend-published facts, not from client session state.

### 6. Frontend: one store, generation-guarded, last-known-good per source

`static/targets.js` (`window.MeshCenterTargets`) is the single controller owning the normalized node+channel targets, the MCA capability overlays, the selected `{kind, id}`, subscription/event notification, and generation tracking. It merges `/api/nodes_management`, the cached `/api/chats` channel projection, `/api/mca/contacts`, and `/api/mca/key-requests`. It never forces radio channel discovery from the Files workspace (the `/api/chats` fetch is the cached projection, no `refresh_channels`), and it never silently replaces a valid source slice with an empty one on a transient failure — each source keeps its own last-known-good slice plus a degraded flag. A concurrent refresh joins the in-flight per-source promise rather than issuing a duplicate fetch, and every merge bumps a `generation` counter consumers can guard on.

The store deliberately has **no epoch/context guard**: there is no in-page context switch to guard against. The only data-context change the store can observe is a radio-profile switch, and that is a full process restart (`_restart_meshcenter_after_profile_switch()` on `/api/node-manager/profiles/<id>/activate`) that ends in a full page reload (`waitForNodeManagerProfile` → `window.location.reload()`), so the store — a per-page singleton — is rebuilt from scratch rather than reused across contexts. Consequently no in-flight fetch can outlive its context, and a `refresh()` always merges the responses it awaits (there is no "drop stale responses" path, which would be a dead guard claiming protection it does not provide). `generation` is the single observable guard for in-page consumers.

### 7. Scope boundary (explicitly out of scope)

This ADR ships the shared model and store, plus the **minimal functional wiring** of the store's selection into the Files workspace (review Finding 1): every node/channel/MCA-contact click goes through the one store (`store.toggleSelect()`); Files subscribes to the store and mirrors a node selection into its counterparty filter; a second click on the selected target deselects it and returns the full transfer list; a channel selection is retained in the store but is never a file counterparty; and the Send dialog pre-selects a recipient only when the selection is a valid, still-sendable node — an invalid/disappeared/channel selection yields an explicit empty placeholder, never a silent fallback to another recipient. Selection state and `aria-pressed` stay synchronized across views because both read the same store.

The desktop Files workspace redesign (PR 5), inline node-card expansion (PR 6), and mobile/narrow-screen behavior (PR 7) consume this model but are not part of it. No new radio channel discovery, no group-encryption model, no change to MCA/1 wire format.

## Consequences

- The four surfaces read one model, so identity (canonical `{kind, id}`, never display name) and capability (the one matrix) can no longer drift between them.
- The "sendable" invariant is enforced in one place; adding a new surface cannot accidentally make a bare node sendable.
- Key-request state is restart-recoverable from the backend (queued commands + persisted timestamps), with the raw timing material never leaving the worker.
- The per-source last-known-good/degraded behavior means a transient `503`/network failure degrades gracefully instead of blanking the workspace.
