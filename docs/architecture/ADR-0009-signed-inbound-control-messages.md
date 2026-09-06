# ADR-0009: Signed inbound control messages (ACK_RECEIVED / ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN)

**Status:** Proposed
**Date:** 2026-09-06
**Context document:** `MCAttach System Design and Implementation Spec v1.2` (sections 9.2, 15.1); ADR-0001 (protocol, `SimpleAckFields`/message types), ADR-0006 (sender state machine), ADR-0008 (attachments backend layer - the worker this ADR's consumers get driven from)

## Context

ADR-0008 wired a real inbound OFFER to `receiver.handle_offer()`. It deliberately did not wire the three "simple ack" message types `receiver.py` already *sends* back to a sender over the wire - `ACK_RECEIVED`, `ACK_DOWNLOADED`, `ACK_PROVIDER_UNKNOWN` (`codec.encode_simple_ack()`, `receiver.py`'s `_maybe_send_ack()`) - to anything that consumes them on the sender's side. Confirmed directly against the current code:

- `sender.py` has `on_ack_received()` (SENT -> RECEIVED) and `on_ack_downloaded()` (RECEIVED -> DOWNLOADED), both taking a plain `attachment_id`, not a raw wire message. Neither has a production caller anywhere - only `tests/test_sender.py` calls them, always with the "correct" preceding state already set up by the test itself.
- `on_ack_downloaded()` requires `state == RECEIVED` exactly and raises `SenderError` otherwise - it has no tolerance for a missed `ACK_RECEIVED` (mesh delivery is not exactly-once or ordered) and no tolerance for being called a second time (both are terminal-adjacent conditions a real wire consumer *will* hit).
- There is no sender-side terminal state for "the recipient's own Provider Registry doesn't recognize the provider I told them to fetch from" - `ACK_PROVIDER_UNKNOWN` is encoded/decoded by `codec.py` and sent by `receiver.py`, but nothing on the sending side has ever needed to represent that outcome, because nothing today applies an incoming one.
- `sender.py` has no `_find_by_transfer_id()`-equivalent lookup (the one `receiver.py` already has, used by `handle_offer()`'s own dedup path) - a sender-side consumer needs the same shape of lookup, scoped to `direction = 'sent'`.
- `attachment_recipients.recipient_principal_id` stores only the recipient's `key_id` (hex) at `create_draft()` time, never their `public_identity` bytes - `sender.py`'s own docstring already establishes this is deliberate (ADR-0008's `AttachmentsService._resolve_recipient_identities()` re-derives it from `key_exchange` bindings at `ENCRYPTING` time for the same reason). No snapshot of the recipient's public key exists anywhere in the schema today.
- `sender.py`'s own `_abandon_transfer_id_and_restart()` (used when an upload session is orphaned - a Relay commit that never got its radio-side OFFER out, for instance) can give one `attachment_id` a *new* `transfer_id`, tombstoning the old one into `mca_tombstones`. Nothing today reads `mca_tombstones` back - only that one write path exists. An ACK signed against the old, now-abandoned `transfer_id` is a real scenario this ADR has to name, not an exotic edge case.

None of this is a defect in ADR-0006/ADR-0008 - both explicitly left "who drives the state machine forward from a wire event" as later work, the same way ADR-0006/ADR-0007 left `network_available`'s source to ADR-0008. This ADR is that answer for the three inbound acks, the same way ADR-0008 was that answer for scheduling.

**Explicitly out of scope:** `CANCEL`, `REJECTED`, `EXPIRED`, `KEY_ROTATE`. All four are encoded/decoded by `codec.py` (round-trip tested) but produced or consumed nowhere in production - not even one-sided, unlike the three acks this ADR covers (which `receiver.py` already sends today). Routing them is deferred to a later ADR once there's a real producer to design the consumer against; this ADR does not touch them and does not claim to make inbound control-message handling complete.

## Decision

### 1. Lookup: `sender._find_by_transfer_id()`, tombstone-aware

New private helper in `sender.py`, mirroring `receiver.py`'s own:

```python
def _find_by_transfer_id(conn: sqlite3.Connection, workspace_id: str, transfer_id_hex: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachments WHERE workspace_id = ? AND transfer_id = ? AND direction = 'sent'",
        (workspace_id, transfer_id_hex),
    ).fetchone()

def _is_tombstoned(conn: sqlite3.Connection, workspace_id: str, transfer_id_hex: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM mca_tombstones WHERE workspace_id = ? AND transfer_id = ? LIMIT 1",
        (workspace_id, transfer_id_hex),
    ).fetchone() is not None
```

A lookup miss is resolved into exactly one of two outcomes, both safe, both silent to the network (no reply is ever sent for a failed/unknown ack - only OFFER handling ever talks back):

- **Unknown entirely** (no row in `attachments`, no row in `mca_tombstones`): either a forged `transfer_id`, or an ack for a transfer this workspace never sent (wrong recipient, replayed from a different workspace's capture, or arrived after a restore-from-backup with a smaller history). Logged at this distinct level, dropped, no state touched.
- **Tombstoned** (`mca_tombstones` has it, `attachments` doesn't - by construction, `_abandon_transfer_id_and_restart()` never deletes the `attachments` row itself, only points it at a new `transfer_id`, so this specific miss shape can only mean the ack raced an orphaned-upload restart): logged as *stale*, not *forged* - genuinely different severity, since this is an expected consequence of retrying a stuck upload, not a hostile input. Dropped the same way; the live `attachment_id` under its new `transfer_id` will get its own, correctly-addressed ack later if the recipient's retry succeeds.

Both cases return `None` from the top-level handler (§4) rather than raising - "malformed/unknown/hostile inbound wire data must not take down the listener" is `mca_runtime.py`'s existing contract (ADR-0008 already established this for `codec.CodecError` on a garbled OFFER); this ADR extends the same contract to acks.

### 2. Key selection: current trusted binding, not a snapshot

The recipient's `public_identity` is resolved the same way ADR-0008's `AttachmentsService._resolve_recipient_identities()` already does: `key_exchange.get_binding_by_key_id(recipient_principal_id)` against the *current* `mca_recipient_bindings` row, where `recipient_principal_id` is `attachment_recipients.recipient_principal_id` for this attachment (Stage 1 scope is `DIRECT`-only with exactly one recipient row per attachment - a `len(recipients) != 1` result is treated as an internal-consistency error, logged and dropped, not guessed at, since multi-recipient delivery does not exist yet for this code path to have been exercised against).

**Rejected alternative: snapshot the recipient's `public_identity` on `attachment_recipients` at `create_draft()` time**, verify acks against the snapshot instead of the live binding. This would make ack verification immune to a key rotation that happens *after* the OFFER was sent but *before* the ack arrives - a real gap in the decision below - at the cost of a new column, a second copy of trust state that can silently drift from `mca_recipient_bindings` (the exact failure mode ADR-0008 fixed for `mca_provider_profiles`' default flag), and having to decide which copy wins if they ever disagree. Rejected for now: the window is small (an ack normally arrives within seconds to minutes of the OFFER, not across a rotation event that itself requires the *sender's own user* to explicitly accept via `accept_pending_key_change()`), and the failure mode when it does happen is safe by construction (§3 below drops an ack that fails verification - it never corrupts state) with a completely ordinary recovery path (the human re-sends). Revisit if real-world use shows this window actually gets hit.

### 3. Verification and idempotent application

New `sender.py` functions. Two layers: `_apply_*` (idempotent, tolerant of loss/reordering, never raises for a *state* reason - only ever called after signature verification already succeeded) wrapping the existing strict `on_ack_received()`/`on_ack_downloaded()` (left completely unchanged - Step 1.4's hardware-tested contract for an *explicit, already-known-valid* call stays exactly as strict as it is today), and one dispatcher per message type that owns lookup + verification (§1, §2) before ever touching state:

```python
def _apply_ack_received(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    """Idempotent/monotonic wrapper around on_ack_received() for wire
    consumption - see this ADR's decision 3."""
    row = _row(conn, attachment_id)
    if row["state"] in (RECEIVED, DOWNLOADED):
        return row["state"]  # already applied, or superseded by ACK_DOWNLOADED already - no-op
    if row["state"] != SENT:
        raise SenderError(f"attachment {attachment_id!r} is {row['state']!r} - ACK_RECEIVED is not a valid event here")
    return on_ack_received(conn, attachment_id, now=now)


def _apply_ack_downloaded(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    """As above, plus the monotonic skip this ADR requires: ACK_DOWNLOADED
    arriving while still SENT means ACK_RECEIVED was lost or reordered on
    the mesh (unordered, unreliable delivery - not a protocol violation) -
    apply both transitions in sequence rather than rejecting."""
    row = _row(conn, attachment_id)
    if row["state"] == DOWNLOADED:
        return DOWNLOADED  # no-op
    if row["state"] == SENT:
        on_ack_received(conn, attachment_id, now=now)
        return on_ack_downloaded(conn, attachment_id, now=now)
    if row["state"] == RECEIVED:
        return on_ack_downloaded(conn, attachment_id, now=now)
    raise SenderError(f"attachment {attachment_id!r} is {row['state']!r} - ACK_DOWNLOADED is not a valid event here")


def _apply_provider_unknown(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    row = _row(conn, attachment_id)
    if row["state"] == PROVIDER_UNKNOWN:
        return PROVIDER_UNKNOWN  # no-op - already recorded from an earlier, possibly duplicate, ack
    if row["state"] != SENT:
        raise SenderError(f"attachment {attachment_id!r} is {row['state']!r} - ACK_PROVIDER_UNKNOWN is not a valid event here")
    now = _now() if now is None else now
    _set_state(conn, attachment_id, PROVIDER_UNKNOWN, now)
    conn.execute("DELETE FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,))
    conn.commit()
    return PROVIDER_UNKNOWN
```

`SenderError` from any `_apply_*` (an ack for a state where it genuinely cannot make sense - e.g. `ACK_DOWNLOADED` while still `ENCRYPTING`, `CANCELLED`, or already terminal-for-another-reason) is caught by the dispatcher (§4), logged, and dropped - it is evidence of either a serious mesh-delivery anomaly or a hostile/replayed frame, not something to crash the listener over. This mirrors `mca_runtime.py`'s existing `RateLimited`/`DeliveryError` handling for key-exchange messages.

**New terminal state: `PROVIDER_UNKNOWN`.** Considered and rejected: folding this into the existing `FAILED_UPLOAD` bucket. Nothing about the upload itself failed - the Relay commit succeeded, the OFFER was delivered - the recipient's own Provider Registry simply doesn't (yet) have this provider profile. Conflating the two would make `FAILED_UPLOAD`'s error_code the only way to tell them apart (already used for actual upload failures - HTTP errors, Relay rejections), which is exactly the "collapsing two different failure domains into one indicator" mistake ADR-0008 called out and refused to make for `RelayStatus`/`UploadReadiness`. `PROVIDER_UNKNOWN` is added to `sender.py`'s state constants and to `TERMINAL_STATES` - a real signature change to `sender.py`'s public state surface, flagged explicitly here rather than folded quietly into "just wiring," since UI code (1.6A/1.6B) will eventually need to recognize it (e.g. to render a distinct "получатель не знает этот провайдер" status, not a generic failure).

### 4. Dispatcher and `mca_runtime.py` wiring

One new public `sender.py` function, `handle_incoming_ack()`, taking the same shape of arguments `receiver.handle_offer()` already does:

```python
def handle_incoming_ack(
    conn: sqlite3.Connection,
    *,
    principal: MCAPrincipal,
    key_exchange: KeyExchangeCoordinator,
    raw_ack: bytes,
    message_type: MessageType,  # codec.MessageType.ACK_RECEIVED / ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN
    now: Optional[float] = None,
) -> Optional[str]:
    """Verify and apply one inbound simple-ack frame against this
    workspace's own 'sent' attachments. Returns the resulting state, or
    None if the frame was dropped (unknown/tombstoned transfer_id, no
    recipient binding to verify against, signature verification failed,
    or the referenced attachment was in a state where this ack made no
    sense) - never raises; every drop reason is logged by this function
    itself at the appropriate level (§1's unknown-vs-tombstoned
    distinction, a verification failure, or a SenderError from an
    _apply_* call), not left to the caller to reconstruct."""
```

`mca_runtime.py`'s `handle_incoming_meshtastic_text()` dispatch (already `codec.peek_message_type()`-based, from ADR-0008) gains one more branch for the three simple-ack types, calling `sender.handle_incoming_ack()` and then `state.service.wake()` if a state changed - same pattern as the OFFER branch. No reply is ever sent back for any of these three (unlike OFFER's KEY_ANNOUNCE-triggering path) - acks are the end of a conversation, not the start of one.

## Consequences

- `sender.py`: new `PROVIDER_UNKNOWN` state (added to `TERMINAL_STATES`); new `_find_by_transfer_id()`, `_is_tombstoned()`, `_apply_ack_received()`, `_apply_ack_downloaded()`, `_apply_provider_unknown()`, `handle_incoming_ack()`. `on_ack_received()`/`on_ack_downloaded()`/`cancel()` are unchanged - still strict, still only ever called (a) directly by tests, or (b) by the new `_apply_*` wrappers after they've already confirmed the transition makes sense.
- No schema migration: `mca_tombstones` already exists (ADR-0003/Step 0.6 era) and gains its first reader; no new column anywhere (§2's rejected alternative is what would have needed one).
- `mca_runtime.py`: `handle_incoming_meshtastic_text()`'s dispatch gains one more `codec.MessageType` branch alongside OFFER (ADR-0008) and key-exchange (Step 1.2/1.3, pre-ADR-0008).
- `CANCEL`/`REJECTED`/`EXPIRED`/`KEY_ROTATE` remain entirely unrouted after this ADR - explicitly out of scope (see Context).
- Key-rotation-during-flight is a named, accepted gap (§2), not silently absorbed - an ack that fails verification is dropped safely, recoverable by the user resending.

## References

- ADR-0001 (protocol - `SimpleAckFields`, `MessageType` enum), ADR-0006 (sender state machine - `on_ack_received`/`on_ack_downloaded`'s original, still-unchanged contracts), ADR-0008 (attachments backend layer - `AttachmentsService.wake()`, `_resolve_recipient_identities()`'s live-binding-lookup precedent this ADR reuses for ack verification, and `mca_runtime.py`'s `codec.peek_message_type()`-based dispatch this ADR extends).
- `receiver.py`'s `_maybe_send_ack()`/`_ack_already_sent()`/`_record_event()` - the existing, symmetric idempotency pattern on the sending-the-ack side; this ADR does not need a matching `attachment_events` row on the *receiving-the-ack* side because `_apply_*`'s own state-based no-op check already gives idempotency without a second bookkeeping table.
- `sender.py`'s `_abandon_transfer_id_and_restart()` and `mca_tombstones` (ADR-0003/Step 0.6 schema) - the existing write path this ADR's lookup has to be aware of.
