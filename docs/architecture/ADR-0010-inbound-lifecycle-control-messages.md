# ADR-0010: Inbound lifecycle control messages (CANCEL / REJECTED / EXPIRED)

**Status:** Accepted
**Date:** 2026-09-11
**Supersedes:** nothing. This ADR completes the three inbound control-message types ADR-0009 explicitly deferred (its "Explicitly out of scope" note named `CANCEL`, `REJECTED`, `EXPIRED`, `KEY_ROTATE`).
**Context documents:** ADR-0001 (protocol semantics, `MessageType` 5/9/10), ADR-0006 (sender state machine), ADR-0007 (receiver state machine), ADR-0009 (signed inbound control messages - the ACK-routing template this ADR mirrors).

## Context

ADR-0009 wired `ACK_RECEIVED` / `ACK_DOWNLOADED` / `ACK_PROVIDER_UNKNOWN` - the three *recipient -> sender* acks that drive a sent attachment forward - through `AttachmentsService._process_inbound_ack()`, and named `CANCEL` / `REJECTED` / `EXPIRED` / `KEY_ROTATE` as the remaining encoded-but-unconsumed types. This ADR routes the three that share the same simple-ack wire shape and map cleanly onto the two state machines. `KEY_ROTATE` stays out of scope: it is not a per-transfer lifecycle signal and needs a different (rotation-specific) handling model, not this ADR's "verify then apply a terminal state" shape.

All three are already fully round-tripped by `codec.py` (`encode_simple_ack` / `decode_simple_ack`, `_SIMPLE_ACK_TYPES` includes them), so no wire change is needed - only the inbound dispatch and state transitions.

**Scope note:** this ADR completes the *inbound consumption* of the three messages. Their *generation* was implemented by the follow-on **ADR-0011 (outbound generation of CANCEL/REJECTED/EXPIRED — durable outbox)**: `_command_reject()` now enqueues a signed `REJECTED`, a bounded reconciliation sweep emits receiver-side `EXPIRED`, and `_command_revoke()` enqueues a signed `CANCEL` for SENT/RECEIVED — each in the same DB transaction as its local state transition, dispatched later with backoff. This ADR's own decisions (the inbound routing, the pinned-identity verification, and the terminal state constants) are unchanged by ADR-0011 and remain as recorded below.

### Semantics (from ADR-0001)

| Type | Direction | Meaning |
|---|---|---|
| `CANCEL` (5) | Sender -> Receiver | Sender withdraws an in-flight offer (the object was revoked or will never be sent). |
| `REJECTED` (9) | Receiver -> Sender | Receiver declined an offer (explicit user "reject"). |
| `EXPIRED` (10) | Receiver -> Sender | Receiver observed the offer's hard expiry pass before it was downloaded. |

Two of the three are `receiver -> sender` and therefore terminate a *sent* row; one is `sender -> receiver` and therefore terminates a *received* row. That asymmetry is the whole of the dispatch design below.

## Decision

### 1. Boundary: same worker, same listener contract, unchanged

The routing boundary stays `AttachmentsService._process_one_inbound_event()`, exactly as ADR-0009 Decision 1. The radio listener still only enqueues an `InboundEvent`. The new dispatch is added inside `_process_one_inbound_event()` after `codec.peek_message_type()`: `REJECTED`/`EXPIRED` join the signed-ACK set (`_INBOUND_ACK_TYPES`), `CANCEL` gets its own receiver-side branch, `OFFER` and key-exchange keep their existing paths.

### 2. REJECTED / EXPIRED reuse the signed-ACK path (`_process_inbound_ack`)

`REJECTED` and `EXPIRED` are `receiver -> sender`, so they verify and apply exactly like an ACK: decode with `verify_key=None`, look up the `direction = 'sent'` row by `transfer_id`, fail closed on a cardinality mismatch, source-route-check against the persisted DIRECT delivery row, verify against the recipient public identity **pinned at `create_draft()` time** (`attachment_recipients.recipient_public_identity`, never the current TOFU binding), then apply. The only change to `_process_inbound_ack()` is the final dispatch: `REJECTED -> sender.apply_rejected()`, `EXPIRED -> sender.apply_expired()`, everything else `sender.apply_ack()`. Every pre-apply step can only drop; there is no radio response (Decision 6).

### 3. CANCEL gets its own receiver-side path (`_process_inbound_cancel`)

`CANCEL` is `sender -> receiver`, so it applies to a `direction = 'received'` row, and the verification is the *mirror* of `_process_inbound_ack()`:

1. `codec.decode_simple_ack(raw, CANCEL, verify_key=None)` -> `transfer_id`.
2. Look up `attachments WHERE workspace_id = ? AND transfer_id = ? AND direction = 'received'`; unknown -> `unknown_transfer` (or `tombstoned_transfer`) drop.
3. Source-route check against the OFFER's persisted **reply route**: `reply_route_type == DIRECT` and `reply_adapter_id` / `reply_connector_profile_id` / `reply_route_id` all equal the envelope's. A CANCEL arriving over any other route is dropped.
4. Verify the CANCEL signature against the sender public identity **pinned on the received row at OFFER admission** (`attachments.sender_public_identity`), never the *current* address binding. The pinned identity is the exact 32-byte key the OFFER's own signature was verified against when it was admitted (or when its parked WAITING_KEY offer was finally resumed), so a later contact-key rotation on that transport address neither breaks a valid CANCEL signed by the original pinned key nor lets the rotated-in key cancel that old transfer.
5. `receiver.apply_cancelled()`.

The trust property is inherited from the OFFER's own admission gate, not re-derived here: `sender_public_identity` is only ever non-NULL when the OFFER was admitted under an `MCA_READY` binding (ADR-0009's trust-gate work - `binding.public_identity if binding_ready else None`, paired with the existing `sender_principal_id = binding.principal_id if binding_ready else None`). A CANCEL for a row whose pinned identity is NULL (a parked WAITING_KEY offer, or a pre-migration row) is unverifiable and dropped; a CANCEL whose signature does not validate against the pinned key is dropped. This deliberately does **not** re-check `binding.status == MCA_READY` on a freshly-resolved binding: the pinned `sender_public_identity` (non-NULL only for a then-ready binding) *is* the trust attestation, and a later rotation must not break the ability of the same original key to withdraw its own offer.

### 4. State transitions

**Sender** (`sender.py`): add `REJECTED` to the state constants and `TERMINAL_STATES`.

- `apply_rejected(conn, attachment_id)` - `SENT`/`RECEIVED -> REJECTED`; any terminal state -> no-op; any pre-send state -> drop (no write). 
- `apply_expired(conn, attachment_id)` - `SENT`/`RECEIVED -> EXPIRED`; any terminal state -> no-op; any pre-send state -> drop. An inbound EXPIRED is only accepted once the sender's authoritative `hard_expires_at` has actually passed (`now >= hard_expires_at`); a frame arriving before that boundary (a recipient whose clock has run ahead, or a replayed/stale frame) is dropped with no state change - the sender's own expiry timestamp, not the recipient's observation of it, is the single source of truth for when an object is genuinely past expiry. At the exact boundary it is accepted.

**Receiver** (`receiver.py`): add `CANCELLED` to the state constants and `TERMINAL_STATES`.

- `apply_cancelled(conn, attachment_id)` - any non-terminal state -> `CANCELLED`; any terminal state -> no-op.

All three are monotonic and idempotent: a duplicate, out-of-order, or stale message never regresses a terminal state and never raises for a state reason (mesh delivery is neither ordered nor exactly-once - the same discipline as ADR-0009 Decision 4).

### 5. Cleanup preserves the retained revoke capability

On the sender transitions, only the *transient* `mca_sender_state` row is retired, via the existing `_delete_transient_sender_state_keeping_revoke()` helper (the same one `ACK_DOWNLOADED` uses, ADR-0009 Decision 5). The retained `mca_sender_revoke_state` row is **preserved** - it holds the durable revoke capability (`revoke_token` / `delete_after`), and a REJECTED or EXPIRED transition must not destroy the ability to later issue a confirmed Relay revoke against a transfer that is (or might still be) committed remotely. The bounded cleanup worker (`AttachmentsService._cleanup_expired_revoke_state()`) is the only thing that removes a revoke row, and it does so solely at `delete_after`; a future confirmed Relay revoke may also remove it. This diverges deliberately from `revoke()`/`cancel()`, which drop both rows only because those are *confirmed local* actions (the Relay revoke already completed before `revoke()` runs; a `cancel()` of a never-sent row has no Relay object at all).

### 6. No radio response - ever

None of the three generates any outbound radio traffic, valid or invalid. `CANCEL`/`REJECTED`/`EXPIRED` are each the end of a conversation, exactly like ADR-0009 Decision 8. Drop reasons are logged via `_drop_ack()` as fixed sanitized tokens only.

### 7. DIRECT-only, and channels are explicitly not recipients

This ADR changes nothing about the routing model. The `envelope.source_address == envelope.route_id` check, the pinned-recipient verification, and the source-route checks remain strictly about **DIRECT node-to-node transfer**. Channels may be added to a *future* shared navigation model, but this ADR does not (and must not) declare a channel to be a file recipient: transferring a file into a channel requires a separate recipient model, trust model, and encryption model that do not exist yet. Nothing here weakens the invariant that plaintext never travels over the mesh and a Relay never sees plaintext.

## Consequences

- `_INBOUND_ACK_TYPES` grows from three to five entries; the comment is updated to record that `CANCEL` is the one sender-signed simple-ack dispatched elsewhere.
- `_process_inbound_cancel()` is a new ~45-line method on `AttachmentsService`, structurally parallel to `_process_inbound_ack()` but verifying sender (not recipient) identity.
- The `attachments` state enums gain `REJECTED` (sender) and `CANCELLED` (receiver), both terminal, both surfaced by the existing snapshots/UI state projections with no state-column schema change.
- Migration 16 adds `attachments.sender_public_identity BLOB` (the pinned sender identity for CANCEL verification), the receiver-side mirror of migration 14's `attachment_recipients.recipient_public_identity`. It is deliberately NOT projected into public snapshots/REST - the snapshot projection is explicit field-by-field, so the raw key stays private by construction (same precedent as `recipient_public_identity`).
- `KEY_ROTATE` remains the last encoded-but-unconsumed message type, still out of scope.
