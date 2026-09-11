# ADR-0010: Inbound lifecycle control messages (CANCEL / REJECTED / EXPIRED)

**Status:** Accepted
**Date:** 2026-09-11
**Supersedes:** nothing. This ADR completes the three inbound control-message types ADR-0009 explicitly deferred (its "Explicitly out of scope" note named `CANCEL`, `REJECTED`, `EXPIRED`, `KEY_ROTATE`).
**Context documents:** ADR-0001 (protocol semantics, `MessageType` 5/9/10), ADR-0006 (sender state machine), ADR-0007 (receiver state machine), ADR-0009 (signed inbound control messages - the ACK-routing template this ADR mirrors).

## Context

ADR-0009 wired `ACK_RECEIVED` / `ACK_DOWNLOADED` / `ACK_PROVIDER_UNKNOWN` - the three *recipient -> sender* acks that drive a sent attachment forward - through `AttachmentsService._process_inbound_ack()`, and named `CANCEL` / `REJECTED` / `EXPIRED` / `KEY_ROTATE` as the remaining encoded-but-unconsumed types. This ADR routes the three that share the same simple-ack wire shape and map cleanly onto the two state machines. `KEY_ROTATE` stays out of scope: it is not a per-transfer lifecycle signal and needs a different (rotation-specific) handling model, not this ADR's "verify then apply a terminal state" shape.

All three are already fully round-tripped by `codec.py` (`encode_simple_ack` / `decode_simple_ack`, `_SIMPLE_ACK_TYPES` includes them), so no wire change is needed - only the inbound dispatch and state transitions.

**Scope note:** this ADR completes the *inbound consumption* of the three messages. Their *generation* is still deferred and explicitly not part of this ADR: `receiver.reject()`/`_command_reject()` transitions the local row but still sends no signed `REJECTED` frame, there is no receiver-side `EXPIRED` emission, and `sender.cancel()`/`sender.revoke()` still send no signed `CANCEL` frame. Closing the round trip (durable outbox generation on top of the consumption added here) is a separate, later piece of work. This ADR does not claim the lifecycle is end-to-end complete - only that the inbound half, which was entirely absent, is now routed and applied.

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
4. Resolve the sender's current binding by address - `key_exchange.get_binding(envelope.route_id)` - and tie its stable `principal_id` to the `sender_principal_id` stored on the received row when the OFFER was admitted. `get_binding()` returns the binding for the DIRECT transport address, which is exactly `envelope.route_id` (per `MeshtasticTextAdapter.ingest()`'s construction); there is no `sender_key_id` field on a simple-ack to resolve by key instead.
5. Verify the CANCEL signature with `VerifyKey(binding.public_identity)`.
6. `receiver.apply_cancelled()`.

The trust property is inherited from the OFFER's own admission gate, not re-derived here: `sender_principal_id` is only ever non-NULL when the OFFER was admitted under an `MCA_READY` binding (ADR-0009's trust-gate work - `binding.principal_id if binding_ready else None`). A CANCEL whose signer is unknown (`get_binding()` -> `None`), or whose `principal_id` no longer matches the stored sender (`KEY_CHANGED` / an unrelated principal now on that address), fails the tie at step 4 and is dropped. This deliberately does **not** re-check `binding.status == MCA_READY`: the stored `sender_principal_id` (non-NULL only for a then-ready binding) *is* the trust attestation, and a later rotation must not break the ability of the same stable principal to withdraw its own offer.

### 4. State transitions

**Sender** (`sender.py`): add `REJECTED` to the state constants and `TERMINAL_STATES`.

- `apply_rejected(conn, attachment_id)` - `SENT`/`RECEIVED -> REJECTED`; any terminal state -> no-op; any pre-send state -> drop (no write). 
- `apply_expired(conn, attachment_id)` - `SENT`/`RECEIVED -> EXPIRED`; any terminal state -> no-op; any pre-send state -> drop.

**Receiver** (`receiver.py`): add `CANCELLED` to the state constants and `TERMINAL_STATES`.

- `apply_cancelled(conn, attachment_id)` - any non-terminal state -> `CANCELLED`; any terminal state -> no-op.

All three are monotonic and idempotent: a duplicate, out-of-order, or stale message never regresses a terminal state and never raises for a state reason (mesh delivery is neither ordered nor exactly-once - the same discipline as ADR-0009 Decision 4).

### 5. Cleanup mirrors a local revoke

On the sender transitions, both secret tables are retired: `DELETE FROM mca_sender_state` and `DELETE FROM mca_sender_revoke_state`. A declined (`REJECTED`) object has no Relay state left worth revoking, and being terminal, `revoke()` refuses to run on the row anyway; an `EXPIRED` object is past hard-expiry + download-grace, so its revoke token has no remaining purpose. This is the same cleanup `revoke()` and `cancel()` already perform, not a new pattern.

### 6. No radio response - ever

None of the three generates any outbound radio traffic, valid or invalid. `CANCEL`/`REJECTED`/`EXPIRED` are each the end of a conversation, exactly like ADR-0009 Decision 8. Drop reasons are logged via `_drop_ack()` as fixed sanitized tokens only.

### 7. DIRECT-only, and channels are explicitly not recipients

This ADR changes nothing about the routing model. The `envelope.source_address == envelope.route_id` check, the pinned-recipient verification, and the source-route checks remain strictly about **DIRECT node-to-node transfer**. Channels may be added to a *future* shared navigation model, but this ADR does not (and must not) declare a channel to be a file recipient: transferring a file into a channel requires a separate recipient model, trust model, and encryption model that do not exist yet. Nothing here weakens the invariant that plaintext never travels over the mesh and a Relay never sees plaintext.

## Consequences

- `_INBOUND_ACK_TYPES` grows from three to five entries; the comment is updated to record that `CANCEL` is the one sender-signed simple-ack dispatched elsewhere.
- `_process_inbound_cancel()` is a new ~45-line method on `AttachmentsService`, structurally parallel to `_process_inbound_ack()` but verifying sender (not recipient) identity.
- The `attachments` state enums gain `REJECTED` (sender) and `CANCELLED` (receiver), both terminal, both surfaced by the existing snapshots/UI state projections with no schema change.
- `KEY_ROTATE` remains the last encoded-but-unconsumed message type, still out of scope.
