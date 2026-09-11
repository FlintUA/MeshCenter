# ADR-0011: Outbound lifecycle control messages (CANCEL / REJECTED / EXPIRED) — durable outbox generation

**Status:** Accepted
**Date:** 2026-09-11
**Supersedes:** the "explicitly deferred" scope note in ADR-0010 (which routed the *inbound* consumption of `CANCEL`/`REJECTED`/`EXPIRED` and named their *generation* as PR 2.5). This ADR is that generation.
**Context documents:** ADR-0001 (protocol, `SimpleAckFields`/`MessageType` 5/9/10), ADR-0006 (sender state machine), ADR-0007 (receiver state machine), ADR-0009 (signed inbound control messages — the pinned-identity and outbox precedent), ADR-0010 (inbound lifecycle control messages — the consumption half this ADR closes the loop on).

## Context

ADR-0010 wired the *inbound* half of the lifecycle: a signed `REJECTED`/`EXPIRED` arriving at the sender applies `sender.apply_rejected()`/`sender.apply_expired()`, and a signed `CANCEL` arriving at the receiver applies `receiver.apply_cancelled()`. It left the *outbound* half unbuilt: `receiver.reject()` transitioned the local row to `REJECTED` but sent no signed `REJECTED` frame; nothing emitted a receiver-side `EXPIRED`; `sender.revoke()` transitioned to `REVOKED` but sent no signed `CANCEL`. A rejection, expiry, or cancellation therefore never made it back to the other side of the transfer.

This ADR closes that loop. It builds on three things that already exist and must not be re-invented:

- **`codec.encode_simple_ack()`** already round-trips all three types and signs with a local Ed25519 key — the frame format is fixed.
- **`mca_outgoing_replies`** (migration 10, hardened in PR #231 review section 4) is already the durable, restart-safe outbox the receiver uses for `ACK_RECEIVED`/`ACK_PROVIDER_UNKNOWN`/`ACK_DOWNLOADED`, dispatched with backoff by `AttachmentsService._dispatch_outgoing_replies()`. It has a `UNIQUE(attachment_id, event_type)` dedup key, a `PENDING`/`SENT`/`UNDELIVERABLE` state column, attempt counters, `next_attempt_at` backoff, and persisted per-source/global rate quotas.
- **The pinned-identity discipline** from ADR-0009/ADR-0010: every frame is signed by the local workspace's MCA principal, verified by the peer against the identity pinned on the transfer at draft/offer time — never a mutable address binding.

The three outbound types are asymmetric in exactly the same way as their inbound counterparts (ADR-0010 §Semantics): two are `receiver -> sender` (generated from a *received* row) and one is `sender -> receiver` (generated from a *sent* row). That asymmetry drives everything below.

## Decision

### 1. One durable outbox, generalized, never a new table

`mca_outgoing_replies` is generalized from "ACK outbox" to "control-message outbox" — no table rename (a rename would buy naming purity at the cost of a risky migration for zero behavior change). The `receiver._maybe_send_ack()` entry point is renamed `receiver.enqueue_control_message()` and now accepts any of the six control types (`ACK_RECEIVED`/`ACK_DOWNLOADED`/`ACK_PROVIDER_UNKNOWN`/`REJECTED`/`EXPIRED`/`CANCEL`) plus an optional `route` argument. The dedup ledger `_ack_already_enqueued()` is renamed `_control_already_enqueued()`; it still means the same thing — "has this `(attachment_id, event_type)` already been *decided*", distinct from "actually transmitted" — but now covers all six types.

The existing dispatch pipeline (`fetch_due_outgoing_replies()` / `check_and_record_reply_quota()` / `mark_reply_sent()` / `mark_reply_attempt_failed()` / `mark_reply_undeliverable()`) is unchanged in shape and reused verbatim for the three new types. A `CANCEL`/`REJECTED`/`EXPIRED` is dispatched, rate-limited, backoff-scheduled, and failed-closed-to-`UNDELIVERABLE` exactly like an ACK already is.

### 2. Migration 17: the immutable route snapshot lives on the outbox row itself

Before this ADR, `fetch_due_outgoing_replies()` `JOIN`ed `attachments` at dispatch time to read `reply_route_type`/`reply_route_id`/... — the route was not frozen on the queued row, and only receiver-side rows existed (so a sender-side `CANCEL`, which must route from `attachment_deliveries`, had no place to read from). Migration 17 adds five columns to `mca_outgoing_replies`:

```
adapter_id, connector_profile_id, route_type, route_id, destination_address
```

`enqueue_control_message()` persists the **complete** route snapshot at enqueue time, and the dispatch step reads *only* those frozen columns — never re-derived from mutable contact data, never `JOIN`ed back to `attachments` (which is joined solely for the workspace filter). For a receiver-side message the snapshot defaults to the attachment's own pinned `reply_*` route; for a sender-side `CANCEL` the caller passes a `ReplyRoute` read from `attachment_deliveries` (which has no `destination_address` column, so for the DIRECT-only MVP it is set equal to `route_id` — the same conflation `_step_ready_to_send()` already makes).

The backfill (`_migration_0017_fixup_backfill_outbox_routes`) copies the `reply_*` route from the owning `attachments` row **only where all five fields are non-NULL**. A pre-existing row with a partial/missing route is left NULL and the dispatch step fails it closed to `UNDELIVERABLE` — never guessed. No pre-migration row can be a sender-side `CANCEL` (that generation does not exist before this ADR), so `attachments.reply_*` is the only source the backfill consults.

A partial route is still enqueued (with the missing fields NULL) rather than rejected at enqueue time — the local transition must never be silently separated from its outbox row — and the dispatch step fails it closed per-field.

### 3. Outbound `REJECTED` — one transaction for transition + enqueue

`_command_reject()` (the `attachment_reject` command) now, in a **single** DB transaction:

1. `receiver.reject(commit=False)` — `WAITING_CONSENT -> REJECTED`.
2. `receiver.enqueue_control_message(REJECTED, event_type=receiver._EVENT_REJECTED_SENT)` — sign with the local MCA principal, insert the PENDING outbox row with the frozen route snapshot.
3. `commit()` once.

The transition + enqueue run inside an explicit SQLite `SAVEPOINT` (`service._atomic`), so the invariant holds in both directions: no local `REJECTED` is committed without a durably-queued `REJECTED` (when a route exists), and a failure *after* the state write but before the enqueue completes — a signing-key load failure, a codec error, any unexpected exception — rolls back both the state change and any partially-inserted outbox row. Such a failure reports the fixed token `command_execution_failed` (never a raw exception). The pre-existing `WAITING_CONSENT` state guard and the outbox `UNIQUE(attachment_id, event_type)` dedup together make a repeat command a no-op in both layers. Radio unavailability does not undo the local rejection — the frame stays `PENDING` and is dispatched later with backoff, surviving restart. The command reports success once the transition + enqueue are durably committed; it does not wait for radio delivery.

### 4. Outbound `EXPIRED` — bounded receiver-side reconciliation

New `receiver.expire()` is the receiver-side hard-expiry transition: when a received row's authoritative `hard_expires_at` has passed, move it to `EXPIRED` and enqueue one signed `EXPIRED` (event_type `receiver._EVENT_EXPIRED_SENT`) — atomically, per row, mirroring `reject()`'s `commit` param. Eligibility is the new `receiver.EXPIRABLE_STATES` (`OFFER_RECEIVED`, `WAITING_KEY`, `WAITING_PROVIDER`, `WAITING_NETWORK`, `WAITING_CONSENT`); `DOWNLOADING`/`VERIFYING` are in-flight and every terminal state is unchanged. The deadline is **fail-closed**: a `hard_expires_at` that is NULL, zero, or negative has no authoritative boundary to have passed, and a `now` before the deadline has not crossed it — either way the state is returned unchanged with no transition, no message, no timeline event.

`AttachmentsService._reconcile_receiver_expiry()` runs once per tick, after the automatic row-scan and before the reply dispatch (so a just-enqueued `EXPIRED` is sent the same tick). It selects received rows in `EXPIRABLE_STATES` with `hard_expires_at IS NOT NULL AND hard_expires_at > 0 AND hard_expires_at <= now`, `ORDER BY created_at ASC LIMIT MAX_RECEIVER_EXPIRY_PER_TICK` (8) — a large backlog drains over successive ticks rather than flushing a matching burst of outbound frames at once. Each row's `expire()` runs in its own `SAVEPOINT` (`service._atomic`), so a row that fails mid-transition (e.g. a signing-key load failure) is rolled back in isolation and later rows still process — the failed row's partial writes (state `UPDATE`, `attachment_events` row, outbox row) are never committed by a later row's commit, and the failure is logged with only the DB-validated attachment id + exception class name. It is otherwise pure SQLite + enqueue (offline), restart-safe, and duplicate-free by construction: `expire()`'s state guard plus the outbox dedup make a re-run a no-op.

### 5. Outbound `CANCEL` — sender revoke, SENT/RECEIVED only

`_command_revoke()` generates a signed `CANCEL` (event_type `sender.CANCEL_EVENT_TYPE = "cancel_sent"`, the one `sender -> receiver` type so it lives in `sender.py` not `receiver.py`) only when the revoke is of a **SENT** or **RECEIVED** row — the receiver has not downloaded the plaintext, so a radio CANCEL can still retract the offer. The `REVOKED` transition and the CANCEL enqueue share one `SAVEPOINT`-bounded transaction (`sender.revoke(commit=False)` + enqueue + `commit()`), with the route snapshot read from `attachment_deliveries` via `_cancel_route_for()`. A failure after the state write but before the enqueue completes (a signing-key load failure, etc.) rolls back the `REVOKED` transition, the sender-state deletion, and any partial outbox row together, reporting the fixed token `command_execution_failed` — the revoke capability is preserved for a retry.

**DOWNLOADED** still completes the remote Relay revoke and the local `REVOKED` transition, but generates **no** radio `CANCEL` — a CANCEL cannot retract plaintext the receiver already holds, so sending one would only tell the receiver to discard an object it already has. A Relay revoke that fails leaves the state unchanged and enqueues nothing (the revoke capability is preserved for a retry); the remote-first ordering from ADR-0009 Decision 5a is unchanged.

### 6. Dispatch hardening: the frozen route is validated strictly, then sent

`_dispatch_outgoing_replies()` validates each queued row's frozen route snapshot before any send, failing closed to `UNDELIVERABLE` (with a fixed sanitized `error_code`) in this order:

1. `route_type`/`route_id` both present, else `reply_route_missing`;
2. `route_type == DIRECT`, else `reply_route_not_direct`;
3. `route_id` and `destination_address` are both canonical `![0-9a-f]{8}`, else `reply_route_invalid_contact`;
4. `destination_address == route_id`, else `reply_destination_mismatch` (a channel or any non-DIRECT shape must never be a send target);
5. `adapter_id` present and equal to the service's live `delivery_adapter.adapter_id`, else `reply_adapter_mismatch`;
6. `connector_profile_id` present and equal to the live `delivery_adapter.connector_profile_id`, else `reply_connector_mismatch`.

Only then does it run the persisted rate quota (`check_and_record_reply_quota()`), `encode()`/`send()` through the live adapter, and `mark_reply_sent()` on a confirmed `DeliveryReceipt.sent is True`. A `send()` that raises records `last_error = delivery_error`, and a send that returns `receipt.sent is False` records `last_error = delivery_receipt_not_sent`; either stays `PENDING` with an attempt counted and backoff scheduled — never falsely marked sent.

### 7. Security: route secrets and raw errors never leave the worker

The three frames are signed with the **local workspace MCA identity** via `codec.encode_simple_ack()` — the same principal already used for ACKs. The route snapshot carried on each outbox row is the five `ReplyRoute` fields only: it never contains the sender's `data_key`/`nonce_prefix`, the `receipt_secret`, a Relay `revoke_token`/`upload_token`, or the peer's raw public identity. The dispatch failure codes are a fixed allowlist of sanitized tokens — `reply_route_missing`, `reply_route_not_direct`, `reply_route_invalid_contact`, `reply_destination_mismatch`, `reply_adapter_mismatch`, `reply_connector_mismatch`, `delivery_error`, `delivery_receipt_not_sent`, plus the command-level `command_execution_failed` — with **no dynamic content appended**: no route id, destination, adapter id, connector profile id, exception message, URL, token, or `repr()` value. Transport-controlled failures are logged with only safe identifiers (the outbox row id) + the exception class name, never `logger.exception()`/traceback on a transport-controlled path; no exception text, no raw adapter error, and no Relay error is ever embedded in a stored or logged reason. This mirrors ADR-0009 Decision 8's "no identifying data in drop reasons" rule.

### 8. DIRECT-only, and channels are still not recipients

Nothing changes about the routing model. `route_type == DIRECT` and `destination_address == route_id` are enforced at dispatch (Decision 6), and the frames are verified by the peer against the pinned DIRECT delivery route (ADR-0010). Channels may be added to a future shared navigation model, but this ADR does not declare a channel to be a file recipient: a channel transfer needs a separate recipient/trust/encryption model that does not exist yet. Nothing here weakens the invariants that plaintext never travels over the mesh and a Relay never sees plaintext.

## Consequences

- **`receiver.py`**: `_maybe_send_ack()`/`_ack_already_enqueued()` renamed to `enqueue_control_message()`/`_control_already_enqueued()` (generalized to six types + `route` param); new `_reply_route_snapshot()`, `EXPIRABLE_STATES`, `_EVENT_REJECTED_SENT`/`_EVENT_EXPIRED_SENT`, `expire()`; `reject()` gains a `commit` param; `fetch_due_outgoing_replies()` reads the frozen route from the outbox row's own columns instead of `JOIN`ing `attachments`.
- **`sender.py`**: `CANCEL_EVENT_TYPE = "cancel_sent"`; `revoke()` and `_set_state()` gain a `commit` param so a transition can fold into a caller-owned transaction.
- **`service.py`**: `_command_reject()` and `_command_revoke()` transition + enqueue in one transaction; `_command_revoke()` gates CANCEL on SENT/RECEIVED; new `_cancel_route_for()` and `_reconcile_receiver_expiry()` (wired into `_tick_locked()` after the row-scan, before dispatch); `_dispatch_outgoing_replies()` gains the six-step route validation (Decision 6).
- **`db/migrations.py`**: migration 17 adds the five route columns to `mca_outgoing_replies` with a fail-closed backfill; `LATEST_VERSION = 17`.
- **`sender.py`'s `REJECTED`/`EXPIRED` and `receiver.py`'s `CANCELLED`** states already existed (ADR-0010); no new state-column schema change.
- No change to `codec.py`, `identity.py`, or the wire format — `encode_simple_ack()`/`decode_simple_ack()` already round-trip all three types.
- `KEY_ROTATE` remains the last encoded-but-unconsumed message type, still out of scope.

## References

- ADR-0001 (protocol — `MessageType` 5/9/10, `SimpleAckFields`), ADR-0006 (sender state machine), ADR-0007 (receiver state machine), ADR-0009 (signed inbound control messages — pinned identity + outbox precedent), ADR-0010 (inbound lifecycle control messages — the consumption half).
- `meshsrv/attachments/receiver.py` — `ReplyRoute`, `enqueue_control_message()`, `fetch_due_outgoing_replies()`, `check_and_record_reply_quota()`, the dispatch-side API this ADR reuses.
- `meshsrv/attachments/sender.py` — `revoke()`/`CANCEL_EVENT_TYPE`, `_step_ready_to_send()`'s `attachment_deliveries` route construction.
- `meshsrv/attachments/db/migrations.py` — migration 10 (`mca_outgoing_replies`) and migration 17 (this ADR's route snapshot).
