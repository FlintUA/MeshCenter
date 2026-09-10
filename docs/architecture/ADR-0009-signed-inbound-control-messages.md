# ADR-0009: Signed inbound control messages (ACK_RECEIVED / ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN)

**Status:** Accepted
**Date:** 2026-09-10
**Supersedes:** the earlier draft of this ADR carried on the historical branch `mcattach-adr-0009-inbound-ack-routing` at commit `7fe4a47`. That draft is reference material only: its four central decisions (verify against the *current* TOFU binding; a *terminal* `PROVIDER_UNKNOWN` attachment state; no `attachment_deliveries` synchronization; and dropping `mca_sender_state` in a way that destroyed the revoke token) are all rejected here, for the reasons recorded under Decision 2, Decision 4, and Decision 5 below.
**Context document:** `MCAttach System Design and Implementation Spec v1.2` (sections 9.2, 15.1); ADR-0001 (protocol, `SimpleAckFields`/`MessageType`), ADR-0006 (sender state machine), ADR-0008 (attachments backend layer - the worker this ADR's consumers get driven from).

## Context

ADR-0008 wired a real inbound OFFER to `receiver.handle_offer()`. It deliberately did not wire the three "simple ack" message types `receiver.py` already *sends* back to a sender over the wire - `ACK_RECEIVED`, `ACK_DOWNLOADED`, `ACK_PROVIDER_UNKNOWN` (`codec.encode_simple_ack()`, `receiver.py`'s `_maybe_send_ack()`) - to anything that consumes them on the sender's side. Confirmed directly against the current code:

- `sender.py` has `on_ack_received()` (SENT -> RECEIVED) and `on_ack_downloaded()` (RECEIVED -> DOWNLOADED), both taking a plain `attachment_id`, not a raw wire message. Neither has a production caller anywhere - only `tests/test_sender.py` calls them, always with the "correct" preceding state already set up by the test itself.
- `on_ack_downloaded()` requires `state == RECEIVED` exactly and raises `SenderError` otherwise - it has no tolerance for a missed `ACK_RECEIVED` (mesh delivery is not exactly-once or ordered) and no tolerance for being called a second time (both are terminal-adjacent conditions a real wire consumer *will* hit).
- There is no sender-side representation of "the recipient's own Provider Registry doesn't recognize the provider I told them to fetch from" - `ACK_PROVIDER_UNKNOWN` is encoded/decoded by `codec.py` and sent by `receiver.py`, but nothing on the sending side has ever applied an incoming one.
- `sender.py` has no `_find_by_transfer_id()`-equivalent lookup scoped to `direction = 'sent'` (the one `receiver.py` already has is for the receive path).
- `attachment_recipients.recipient_principal_id` stores only the recipient's `key_id` (hex) at `create_draft()` time, never their `public_identity` bytes - no snapshot of the recipient's public key exists anywhere in the schema today.
- `sender.py`'s `_abandon_transfer_id_and_restart()` can give one `attachment_id` a *new* `transfer_id`, tombstoning the old one into `mca_tombstones`. Nothing today reads `mca_tombstones` back. An ACK signed against the old, now-abandoned `transfer_id` is a real scenario this ADR has to name, not an exotic edge case.

None of this is a defect in ADR-0006/ADR-0008 - both explicitly left "who drives the state machine forward from a wire event" as later work. This ADR is that answer for the three inbound acks, the same way ADR-0008 was that answer for scheduling.

**Explicitly out of scope:** `CANCEL`, `REJECTED`, `EXPIRED`, `KEY_ROTATE`. All four are encoded/decoded by `codec.py` (round-trip tested) but produced or consumed nowhere in production. Routing them is deferred to a later ADR; this ADR does not touch them and does not claim to make inbound control-message handling complete.

## Decision

### 1. Boundary: the worker owns everything; the listener only enqueues

The inbound routing boundary is `AttachmentsService._process_one_inbound_event()`, unchanged from ADR-0008's placement. The radio listener (`mca_runtime.handle_incoming_meshtastic_text()`) continues to do exactly one thing: build an `InboundEvent` and `enqueue_inbound()` it. The listener never acquires the tick lock, never opens/queries SQLite, never decodes CBOR, never verifies a signature, never calls Relay HTTP, and never waits for the worker. The new ACK dispatch (Decision 7) is added inside `_process_one_inbound_event()` - after `codec.peek_message_type()` and before the OFFER/key-exchange split - so the three ACK types are routed to the new signed-ACK path, OFFER continues its existing path, and key-exchange messages continue their existing path.

### 2. Trust: verify against the recipient public identity pinned to this exact transfer

The ACK is verified with the recipient's Ed25519 `public_identity` **as pinned at `create_draft()` time on that transfer's own `attachment_recipients` row**, never against the *current* `mca_recipient_bindings` row.

The recipient `public_identity` is 32 raw bytes already resolved by the caller into `RecipientTarget` before `create_draft()` (ADR-0006/ADR-0008). `create_draft()` now persists those bytes on `attachment_recipients.recipient_public_identity` (new column, Decision 6). At ACK-verification time the worker reads that pinned value, derives its `key_id` via `identity.compute_key_id()`, and requires it to equal the stored `recipient_principal_id` - then verifies the ACK signature with `VerifyKey(recipient_public_identity)`.

This is the inverse of the historical draft's decision. The historical draft verified against `get_binding_by_key_id()` (the *current* TOFU binding), which made an ACK verify against whichever key the recipient currently has - not necessarily the key the OFFER's envelope was actually sealed to. The failure mode this ADR closes: after a trusted contact-key rotation, a valid ACK signed by the *old* pinned key (the key the transfer was sealed to) must still be accepted, while an ACK signed only by the *new* current key must be rejected for the old transfer. Verifying against the current binding gets exactly this backwards. `get_binding_by_key_id()` is therefore **never called** on the ACK path.

A verification failure is a drop with no state change and no radio reply (Decision 8); the transfer can be re-driven by a fresh OFFER under a fresh transfer_id if the human re-sends.

### 3. Source routing: accept an ACK only from the persisted DIRECT delivery route

An ACK is only ever applied if it arrives on the exact delivery route the OFFER was sent over, as persisted on the attachment's own `attachment_deliveries` row. Concretely the worker requires all of:

- exactly one `attachment_recipients` row **and** exactly one `attachment_deliveries` row for the attachment (Stage 1 scope - Decision 5 - makes a cardinality mismatch an internal-consistency failure to drop on, never to guess around);
- `attachment_deliveries.route_type == DIRECT`;
- `attachment_deliveries.adapter_id == envelope.adapter_id`;
- `attachment_deliveries.connector_profile_id == envelope.connector_profile_id`;
- `attachment_deliveries.route_id == envelope.route_id` (the adapter-normalized inbound source address; for the DIRECT-only MVP this is exactly `event.source_address`, per `MeshtasticTextAdapter.ingest()`'s own construction).

This prevents an ACK for a given transfer from being accepted through any route other than the one that transfer actually used - the same shape of check `_resolve_recipient_identities()` already applies on the send path, now mirrored on the receive-ack path.

### 4. Monotonic, idempotent, atomic application

The three ACK types apply to a sender row atomically in a single transaction. Application is monotonic and idempotent - an ACK arriving out of order or more than once (mesh delivery is neither ordered nor exactly-once) never regresses state and never raises for a *state* reason. The full matrix:

- **ACK_RECEIVED**
  - `SENT` -> `RECEIVED`: set `received_at` (if null) on the recipient row, delivery `state = RECEIVED` with `ack_at` (if null), `attachments.state = RECEIVED`, clear any `error_code`, record one timeline event.
  - `RECEIVED` -> no-op (duplicate).
  - `DOWNLOADED` -> no-op (already superseded; never regress).
  - any other state -> drop (no write).
- **ACK_DOWNLOADED**
  - `SENT` -> apply the RECEIVED transition, then -> `DOWNLOADED` (ACK_RECEIVED was lost/reordered on the mesh - a normal, unordered delivery, not a protocol violation).
  - `RECEIVED` -> `DOWNLOADED`.
  - `DOWNLOADED` -> no-op (duplicate).
  - any other state -> drop.
  - On the transition: set `received_at`/`downloaded_at` (if null), delivery `state = DOWNLOADED` with `ack_at` (if null), `attachments.state = DOWNLOADED`, clear errors, delete the transient `mca_sender_state` row **only after** the revoke capability is durably retained (Decision 5), preserve `mca_sender_revoke_state`, record timeline events exactly once.
- **ACK_PROVIDER_UNKNOWN**
  - `SENT` -> keep `attachments.state = SENT` (no terminal state - Decision 4a), set non-terminal `attachments.error_code = recipient_provider_unknown`, delivery `state = PROVIDER_UNKNOWN` with `error_code = recipient_provider_unknown` and `ack_at` (if null), retain upload + revoke state, record one timeline event. Idempotent: if the delivery is already `PROVIDER_UNKNOWN`, no-op.
  - `RECEIVED`/`DOWNLOADED` -> ignore (never regress).
  - any other state -> drop.

Because the worker is the sole writer, "atomic" is the plain `sender.apply_ack()` transaction on the worker's own connection - the same one-transaction discipline every `sender._set_state()` transition already uses.

**4a. `ACK_PROVIDER_UNKNOWN` is non-terminal.** The upload did not fail - the Relay commit succeeded and the OFFER was delivered; the recipient's own Provider Registry merely doesn't know the provider this workspace told it to fetch from. Folding that into `FAILED_UPLOAD` would collapse two different failure domains into one indicator. It is represented as a non-terminal `error_code` on a row that stays `SENT`, so a later `ACK_RECEIVED`/`ACK_DOWNLOADED` (the recipient fetched the profile in the meantime) still transitions cleanly. No terminal `PROVIDER_UNKNOWN` state is added to the sender state machine. (The historical draft's terminal `PROVIDER_UNKNOWN` state is explicitly rejected.)

### 5. Split sender-secret lifecycle: transient state vs. retained revoke capability

`mca_sender_state` today holds *two* classes of secret in one row: transient encryption/upload material (`data_key`, `nonce_prefix`, `manifest_blob`, `manifest_sha256`, `upload_id`, `upload_token`) that is useless once the recipient has downloaded the object, and the `revoke_token` that remains necessary **after** download to revoke the Relay object (Step 1.6A.5's revoke). Deleting the row on `ACK_DOWNLOADED` (as the historical draft and the current `on_ack_downloaded()` both do) destroys the revoke token and makes a post-download revoke impossible.

The lifecycle is split:

- **`mca_sender_state`** remains the transient row: encryption keys, manifest, and upload credentials. It is deleted once the object is downloaded (`ACK_DOWNLOADED`) or the transfer is confirmed `cancel`led/`revoke`d/`EXPIRED` - i.e. once those secrets have no remaining purpose.
- **`mca_sender_revoke_state`** (new table, Decision 6) retains the *minimum* revoke capability - `revoke_token` only - until the object is confirmed revoked or cancelled, or a bounded `delete_after` expires. The row is created at the `READY_TO_SEND` transition (the first point both `revoke_token` and `hard_expires_at` are known) and is **not** deleted by `ACK_DOWNLOADED`. It is deleted only by a confirmed `cancel`/`revoke` (Decision 5a) or by the worker's bounded cleanup once `delete_after` has passed (`delete_after` = `hard_expires_at + download_grace_seconds`, the moment the Relay object is guaranteed gone).

The ordering invariant that makes this crash-safe: the revoke row is always written durably **before** the transient row can be deleted on `ACK_DOWNLOADED`. On the `ACK_DOWNLOADED` path the worker defensively re-materializes the revoke row (from `mca_sender_state.revoke_token` + the attachment's `hard_expires_at`/`download_grace_seconds`) if it is somehow absent, before deleting `mca_sender_state`.

**5a. Revoke/cancel compatibility.** The revoke command reads the `revoke_token` from `mca_sender_revoke_state` first, falling back to `mca_sender_state` only for rows that predate the migration (which copies existing tokens into the new table - Decision 6). Confirmed cancel/revoke deletes **both** tables. A revoke that can find no retained token (post-download, pre-migration edge) fails `relay_unreachable` - it never falsely reports success and never deletes the last retained token on an unconfirmed remote failure.

### 6. Migration 14: pinned identity + retained revoke state

The next sequential migration (14, `LATEST_VERSION` was 13) makes two schema changes, both additive and fail-closed:

- **`attachment_recipients.recipient_public_identity`** - a nullable `BLOB` column. New drafts persist the exact 32-byte `RecipientTarget.public_identity` (immutable thereafter); a `create_draft()` that is handed anything other than 32 bytes, a derived `compute_key_id(public_identity)` that does not equal the recipient's `key_id`, or (in Stage 1) a draft whose recipients are not exactly one, fails closed with `SenderError`. Existing rows are **not** backfilled: a pre-migration row with a `NULL` identity is an unverifiable transfer, and an inbound ACK for it is dropped (Decision 2 fails closed), never guessed at.
- **`mca_sender_revoke_state`** - a new table:

  ```sql
  CREATE TABLE mca_sender_revoke_state (
      attachment_id TEXT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,
      revoke_token TEXT NOT NULL,
      delete_after TEXT NOT NULL,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
  );
  ```

  Timestamps are stored as **Unix epoch seconds rendered as decimal `TEXT`** (the task's schema fixes the column type as `TEXT`; a decimal-string epoch keeps the derivation from the already-integer `hard_expires_at` transparent and the cleanup comparison trivial via `CAST(delete_after AS INTEGER)`). The up-migration's `data_fixup` copies every non-null `revoke_token` from `mca_sender_state` into the new table, deriving `delete_after` from the attachment's `hard_expires_at + download_grace_seconds` (with a conservative `now + DEFAULT_HARD_TTL_SECONDS` fallback for an in-flight row whose commit hasn't reported a `hard_expires_at` yet). `mca_sender_revoke_state` is added to `ALL_TABLE_NAMES`.

### 7. Routing and verification sequence

New `sender.apply_ack(conn, attachment_id, message_type, now)` implements Decision 4's matrix. The worker-side handler `_process_inbound_ack()` implements the verification sequence, every step of which precedes any durable write and none of which ever produces a radio response:

1. `codec.decode_simple_ack(raw, message_type, verify_key=None)` to reach the 16-byte `transfer_id` (through the existing size/format guards).
2. Look up the outgoing attachment by `transfer_id` (`direction = 'sent'`, this workspace). A miss resolves to exactly one drop: `mca_tombstones` has it -> sanitized info + drop (a stale ack racing an orphaned-upload restart); otherwise -> sanitized warning + drop (unknown/forged).
3. Cardinality: exactly one recipient row and one delivery row, else fail closed.
4. Source route (Decision 3).
5. Pinned key (Decision 2): `recipient_public_identity` present and 32 bytes, derived `key_id == recipient_principal_id`, then `decode_simple_ack(..., VerifyKey(recipient_public_identity))`.
6. `sender.apply_ack(...)` in one transaction.

### 8. No radio response, ever

For **any** ACK - valid, invalid, unknown, tombstoned, duplicate, or stale - the worker sends nothing back over the radio. ACKs are the end of a conversation, not the start of one (unlike OFFER, which triggers a KEY_ANNOUNCE). Every drop reason is logged at a sanitized level that omits the raw source address, key bytes/key IDs, signatures, transfer/attachment IDs, tokens, ciphertext, manifest, local paths, and exception text - a fixed reason token (`not_well_formed`, `unknown_transfer`, `tombstoned_transfer`, `cardinality`, `source_route_mismatch`, `pinned_key_missing`, `pinned_key_mismatch`, `signature_invalid`, `state_drop`) only. Malformed input is caught per-event inside `_drain_inbound_events()`'s existing guard and can never escape the worker tick or terminate the worker.

## Consequences

- **`sender.py`**: `create_draft()` persists `recipient_public_identity` (fail-closed); `on_ack_received()`/`on_ack_downloaded()` are replaced by `apply_ack()` (the matrix); `_step_uploading()`'s `READY_TO_SEND` transition materializes `mca_sender_revoke_state`; `cancel()`/`revoke()` delete both secret tables; new `revoke_delete_after()`/`revoke_state_ts()` helpers derive the retention bound and its on-disk encoding. No terminal `PROVIDER_UNKNOWN` state is introduced.
- **`service.py`**: `_process_one_inbound_event()` gains the ACK dispatch; new `_process_inbound_ack()` implements Decision 7; `_command_revoke()` reads the revoke token from `mca_sender_revoke_state` first (fallback to `mca_sender_state`); `_tick_locked()` gains a bounded `_cleanup_expired_revoke_state()` step.
- **`db/migrations.py`**: Migration 14 (`attachment_recipients.recipient_public_identity` + `mca_sender_revoke_state` + backfill `data_fixup`); `ALL_TABLE_NAMES` extended.
- No change to `receiver.py`: the receiver already signs ACKs with its own principal key, which is exactly the pinned identity the sender now verifies against.
- `CANCEL`/`REJECTED`/`EXPIRED`/`KEY_ROTATE` remain unrouted - explicitly out of scope.

## References

- ADR-0001 (protocol - `SimpleAckFields`, `MessageType`, `mca_tombstones` semantics), ADR-0006 (sender state machine), ADR-0008 (attachments backend layer - `AttachmentsService._process_one_inbound_event()`, `_resolve_recipient_identities()`, the listener-enqueue/worker-ingest boundary).
- `receiver.py`'s `_maybe_send_ack()` - the existing sender-side of these ACKs, signing with the receiver's own principal key.
- `sender.py`'s `_abandon_transfer_id_and_restart()` and `mca_tombstones` - the existing write path Decision 7's lookup must be aware of.
- `identity.compute_key_id()` - the key_id derivation the pinned-key check reuses.
