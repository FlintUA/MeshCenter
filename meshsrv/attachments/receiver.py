"""meshsrv/attachments/receiver.py

The receiver-side state machine for incoming MCA attachments (Execution
Plan Step 1.5; design spec sections 13, 15.2; ADR-0007 for the design
decisions this module implements: descriptor-signature verification,
manifest AAD bootstrap from the signed descriptor, and the
sender_key_id -> public_identity lookup).

State machine (design spec 15.2):

    OfferReceived -> WaitingKey | WaitingProvider | WaitingNetwork | WaitingConsent
    WaitingKey       -> WaitingProvider | WaitingNetwork | WaitingConsent
    WaitingProvider  -> WaitingConsent | WaitingNetwork
    WaitingNetwork   -> WaitingConsent
    WaitingConsent   -> Downloading            (explicit user action only)
    Downloading      -> Verifying | WaitingNetwork (transient error)
    Verifying        -> Available | Failed
    {OfferReceived, WaitingKey, WaitingProvider, WaitingNetwork} -> Expired
    WaitingConsent   -> Rejected

`Expired` is not swept automatically in this pass (no expiry sweep worker
exists yet, same scope boundary `sender.py` drew for its own `Expired`/
`Revoked` states) - the state constant exists for the schema/UI to use
once Step 1.8 (TTL/cleanup) adds the sweep.

Two entry points, mirroring `key_exchange.py`'s "holds no transport of its
own" convention rather than `sender.py`'s "calls delivery_adapter.send()
itself" convention - an inbound OFFER is architecturally an event to react
to, not a job this module owns end-to-end:

  - `handle_offer()` - call once per inbound OFFER `DeliveryEnvelope`'s
    already-`ingest()`-ed logical bytes. Decodes unverified first (a
    first-contact OFFER's signer may not be known yet), dedups by
    `(workspace_id, transfer_id)`, resolves the signer via
    `key_exchange.get_binding_by_key_id()`, and - if a binding exists -
    verifies the signature before ever creating a row. Then chains into
    `run_step()` to advance as far as automatic logic allows.
  - `run_step()` - advance one attachment from its current state as far
    as automatic logic allows right now. This is also what a later
    reconciliation pass (`reconcile_pending()`, mirroring
    `sender.resume_pending()`) calls for every non-terminal received
    attachment after an external event (a new KEY_ANNOUNCE bound, a
    provider registered, network restored) - satisfying the Step 1.5 DoD
    that a pending WAITING_PROVIDER offer becomes available after a
    manual provider import without any repeat radio delivery, and the
    symmetric case for WAITING_KEY once a binding for its sender_key_id
    appears (design spec 15.2's "ключ получен" edges - see migration 6's
    `pending_offer_cbor` column for how this is done without needing the
    sender to re-deliver the OFFER).

Both return a `ReceiveResult(attachment_id, state, replies)` - `replies`
are encoded-and-signed logical MCA frames (ACK_RECEIVED/
ACK_PROVIDER_UNKNOWN/ACK_DOWNLOADED) the caller still has to
`encode()`/`send()` through whichever adapter/route the triggering
envelope arrived on - this module never imports `DeliveryAdapter` or
sends anything itself (`key_exchange.py`'s own docstring: "holds no
transport of its own").

ACK dedup (ADR-0007 decision 5): each of the three ACK types is sent at
most once per `attachment_id`, gated by a prior `attachment_events` row of
that type - not a time-window quota (`key_exchange.py`'s quota table
solves a different problem: bounding a stranger's ability to trigger
unlimited announces from many addresses, which does not apply to an
already-deduplicated `transfer_id`).

`Downloading` always restarts from scratch on any interruption - no
chunk-level resume in this pass (ADR-0007 decision 6, symmetric with the
sender's own MVP upload-restart simplification).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import time
import uuid
from typing import Callable, List, Optional

from nacl.signing import VerifyKey

from meshsrv.attachments import codec, crypto, identity, manifest
from meshsrv.attachments.identity import MCAPrincipal
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.provider_registry import ProviderRegistry, encode_provider_id
from meshsrv.attachments.relay_client import (
    RelayClient,
    RelayError,
    RelayHTTPError,
    RelayUnavailableError,
    RelayVerificationError,
    verify_descriptor_signature,
)
from meshsrv.attachments.workspace import MCAWorkspaceManager

# ---- state constants (design spec 15.2) ------------------------------------

OFFER_RECEIVED = "OFFER_RECEIVED"
WAITING_KEY = "WAITING_KEY"
WAITING_PROVIDER = "WAITING_PROVIDER"
WAITING_NETWORK = "WAITING_NETWORK"
WAITING_CONSENT = "WAITING_CONSENT"
DOWNLOADING = "DOWNLOADING"
VERIFYING = "VERIFYING"
AVAILABLE = "AVAILABLE"
EXPIRED = "EXPIRED"
REJECTED = "REJECTED"
FAILED = "FAILED"

TERMINAL_STATES = frozenset({AVAILABLE, EXPIRED, REJECTED, FAILED})
# States run_step() can make forward progress on by itself, without an
# explicit user action (WAITING_CONSENT needs `begin_download()`).
AUTOMATIC_STATES = frozenset({WAITING_KEY, WAITING_PROVIDER, WAITING_NETWORK, DOWNLOADING})

_ACK_RECEIVED_TYPE = codec.MessageType.ACK_RECEIVED
_ACK_PROVIDER_UNKNOWN_TYPE = codec.MessageType.ACK_PROVIDER_UNKNOWN
_ACK_DOWNLOADED_TYPE = codec.MessageType.ACK_DOWNLOADED

_EVENT_ACK_RECEIVED_SENT = "ack_received_sent"
_EVENT_ACK_PROVIDER_UNKNOWN_SENT = "ack_provider_unknown_sent"
_EVENT_ACK_DOWNLOADED_SENT = "ack_downloaded_sent"

DEFAULT_DOWNLOAD_GRACE_SECONDS = 3600  # design spec 14: download_grace = 1h (mirrors sender.py's default)

# ---- outgoing ACK outbox (PR #227 defect #1) -------------------------------
# `mca_outgoing_replies` (migration 10) is the real, persisted, retryable
# queue nothing existed for before this fix - see migrations.py's own
# comment above _MIGRATION_0010_UP for the full defect this replaces.
# Backoff shape mirrors ConnectivityMonitor's own Relay health backoff:
# doubling from a base, capped at a ceiling, retried forever rather than
# ever giving up (an ACK the sender is still waiting on stays worth
# retrying for as long as this attachment itself is retained).
OUTGOING_REPLY_BACKOFF_BASE_SECONDS = 30
OUTGOING_REPLY_BACKOFF_CEILING_SECONDS = 3600
MAX_OUTGOING_REPLY_SENDS_PER_DISPATCH = 5  # rate limit: drain the queue gradually, not all at once


class ReceiverError(RuntimeError):
    """Base class for every error this module raises directly. Failures
    from a downloaded object's own content (bad signature, bad AEAD, bad
    digest) are never raised as this type - they are caught internally
    and turned into a FAILED transition, since a malformed/tampered
    object is an expected, handled outcome for this module, not a bug."""


@dataclasses.dataclass(frozen=True)
class ReceiveResult:
    attachment_id: str
    state: str
    replies: List[bytes] = dataclasses.field(default_factory=list)


# ---- small local helpers (module-private, mirroring sender.py's style) ----


def _now() -> float:
    return time.time()


def _row(conn: sqlite3.Connection, attachment_id: str) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
    if row is None:
        raise ReceiverError(f"no attachment {attachment_id!r}")
    return row


def get_state(conn: sqlite3.Connection, attachment_id: str) -> str:
    return _row(conn, attachment_id)["state"]


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def _find_by_transfer_id(conn: sqlite3.Connection, workspace_id: str, transfer_id_hex: str) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT * FROM attachments WHERE workspace_id = ? AND transfer_id = ? AND direction = 'received'",
        (workspace_id, transfer_id_hex),
    ).fetchone()


def _record_event(conn: sqlite3.Connection, attachment_id: str, now: float, event_type: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO attachment_events (id, attachment_id, occurred_at, event_type, detail_json) VALUES (?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, attachment_id, now, event_type, json.dumps(detail)),
    )


def _ack_already_enqueued(conn: sqlite3.Connection, attachment_id: str, event_type: str) -> bool:
    """`mca_outgoing_replies` is now the single idempotency ledger for
    "has this ACK been handled" - a row existing here (PENDING, SENT, or
    UNDELIVERABLE) means this call already decided to send it once, no
    matter whether the real transmission has happened yet. Renamed from
    `_ack_already_sent()`/its old `attachment_events`-backed check
    (PR #227 defect #1): that name/table pairing conflated "we decided to
    send this" with "this was actually transmitted", which is exactly
    the ambiguity that let this module claim an ACK was sent when it
    never left the process."""
    row = conn.execute(
        "SELECT 1 FROM mca_outgoing_replies WHERE attachment_id = ? AND event_type = ? LIMIT 1",
        (attachment_id, event_type),
    ).fetchone()
    return row is not None


def _set_state(
    conn: sqlite3.Connection,
    attachment_id: str,
    new_state: str,
    now: float,
    *,
    error_code: Optional[str] = None,
    extra_sql: str = "",
    extra_params=(),
) -> None:
    conn.execute(
        f"UPDATE attachments SET state = ?, error_code = ? {extra_sql} WHERE id = ?",
        (new_state, error_code, *extra_params, attachment_id),
    )


def _maybe_send_ack(
    conn: sqlite3.Connection,
    *,
    attachment_id: str,
    transfer_id: bytes,
    message_type: codec.MessageType,
    event_type: str,
    principal: MCAPrincipal,
    workspace_manager: MCAWorkspaceManager,
    now: float,
) -> Optional[bytes]:
    """Enqueue `message_type` at most once per `attachment_id` (ADR-0007
    decision 5 - unchanged) into the real `mca_outgoing_replies` outbox
    (PR #227 defect #1) instead of handing the encoded frame to a caller
    that (in production) never actually transmitted it. Returns the
    encoded frame that was newly enqueued this call, or `None` if this
    event_type was already enqueued before - same observable return
    contract callers/tests already depend on, so `ReceiveResult.replies`
    still means "how many ACK types this call newly decided to send",
    even though *sending* now happens later, out of this call entirely,
    via AttachmentsService's own dispatch step
    (`fetch_due_outgoing_replies()`/`mark_reply_sent()`/
    `mark_reply_attempt_failed()` below).

    Deliberately does NOT record any "sent" bookkeeping here - that used
    to happen via `_record_event()` in this exact spot, before any
    transmission was attempted at all (the commit-before-send ordering
    bug this fix closes). The outbox row this inserts starts, and stays,
    PENDING until the dispatch step's own `mark_reply_sent()` call
    confirms a real `DeliveryAdapter.send()` succeeded."""

    if _ack_already_enqueued(conn, attachment_id, event_type):
        return None
    signing_key = identity.load_signing_key(workspace_manager, principal)
    ack = codec.encode_simple_ack(message_type, transfer_id, signing_key)
    conn.execute(
        """
        INSERT INTO mca_outgoing_replies
            (id, attachment_id, event_type, message, state, attempts, created_at, next_attempt_at)
        VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)
        """,
        (uuid.uuid4().hex, attachment_id, event_type, ack, now, now),
    )
    return ack


# ---- outgoing ACK outbox: dispatch-side API (PR #227 defect #1) -----------
# Called only by AttachmentsService's own dispatch step (service.py owns
# the DeliveryAdapter this module never imports - module docstring). These
# functions only ever touch `mca_outgoing_replies`/`attachments` rows, the
# same persistence boundary every other function in this module keeps.


@dataclasses.dataclass(frozen=True)
class PendingReply:
    """One row due for a real send attempt. `route_type`/`route_id` come
    from the owning attachment's own `reply_route_type`/`reply_route_id`
    (set once, at `handle_offer()` time) - `None` for either means no
    route was ever recorded for this attachment (a caller that ran
    `handle_offer()` without `source_address`, e.g. this module's own
    non-integration tests), which the dispatch step treats as permanently
    undeliverable rather than retrying forever for no reason."""

    id: str
    attachment_id: str
    event_type: str
    message: bytes
    attempts: int
    route_type: Optional[str]
    route_id: Optional[str]


def fetch_due_outgoing_replies(
    conn: sqlite3.Connection, workspace_id: str, now: float, limit: int = MAX_OUTGOING_REPLY_SENDS_PER_DISPATCH
) -> List[PendingReply]:
    """Up to `limit` PENDING replies whose `next_attempt_at` has arrived,
    oldest-created first - the rate limit (PR #227 defect #1) that keeps
    a burst of queued ACKs from being flushed onto the radio all at once
    in a single dispatch pass; the rest simply wait for the next tick."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT r.id AS id, r.attachment_id AS attachment_id, r.event_type AS event_type, r.message AS message,
               r.attempts AS attempts, a.reply_route_type AS route_type, a.reply_route_id AS route_id
        FROM mca_outgoing_replies AS r
        JOIN attachments AS a ON a.id = r.attachment_id
        WHERE a.workspace_id = ? AND r.state = 'PENDING' AND r.next_attempt_at <= ?
        ORDER BY r.created_at ASC
        LIMIT ?
        """,
        (workspace_id, now, limit),
    ).fetchall()
    return [
        PendingReply(
            id=row["id"], attachment_id=row["attachment_id"], event_type=row["event_type"], message=row["message"],
            attempts=row["attempts"], route_type=row["route_type"], route_id=row["route_id"],
        )
        for row in rows
    ]


def mark_reply_sent(conn: sqlite3.Connection, reply_id: str, now: float) -> None:
    """Called only after `DeliveryAdapter.send()` has actually returned
    successfully - never before (that ordering is the whole point of
    this fix). Also records the same `attachment_events` entry the old,
    premature call used to (event_type unchanged), so anything downstream
    already reading that table for audit/history purposes keeps working,
    now at the point transmission is truly confirmed instead of merely
    attempted."""
    row = conn.execute(
        "SELECT attachment_id, event_type FROM mca_outgoing_replies WHERE id = ?", (reply_id,)
    ).fetchone()
    conn.execute(
        "UPDATE mca_outgoing_replies SET state = 'SENT', sent_at = ? WHERE id = ?",
        (now, reply_id),
    )
    if row is not None:
        _record_event(conn, row[0], now, row[1], {})
    conn.commit()


def mark_reply_attempt_failed(conn: sqlite3.Connection, reply_id: str, now: float, *, error_code: str) -> None:
    """A real send attempt failed (transport/adapter raised). Stays
    PENDING - retried on a later dispatch, never lost - with `attempts`
    incremented and `next_attempt_at` pushed out by an exponential
    backoff capped at `OUTGOING_REPLY_BACKOFF_CEILING_SECONDS`, the same
    shape ConnectivityMonitor uses for Relay health checks."""
    row = conn.execute("SELECT attempts FROM mca_outgoing_replies WHERE id = ?", (reply_id,)).fetchone()
    attempts = (row[0] if row is not None else 0) + 1
    backoff = min(
        OUTGOING_REPLY_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)),
        OUTGOING_REPLY_BACKOFF_CEILING_SECONDS,
    )
    conn.execute(
        "UPDATE mca_outgoing_replies SET attempts = ?, next_attempt_at = ?, last_error = ? WHERE id = ?",
        (attempts, now + backoff, error_code[:200], reply_id),
    )
    conn.commit()


def mark_reply_undeliverable(conn: sqlite3.Connection, reply_id: str, now: float, *, error_code: str) -> None:
    """No route was ever recorded for this reply's attachment (see
    `PendingReply`'s own docstring) - there is nothing to retry towards,
    so this is a terminal outcome for the row, not a backoff case."""
    conn.execute(
        "UPDATE mca_outgoing_replies SET state = 'UNDELIVERABLE', last_error = ? WHERE id = ?",
        (error_code[:200], reply_id),
    )
    conn.commit()


def _resolve_provider_or_wait(
    conn: sqlite3.Connection,
    *,
    attachment_id: str,
    provider_id_text: str,
    provider_registry: ProviderRegistry,
    network_available: bool,
    now: float,
    on_provider_unknown: Optional[Callable[[], Optional[bytes]]] = None,
) -> ReceiveResult:
    """Shared WAITING_PROVIDER/WAITING_NETWORK/WAITING_CONSENT routing,
    used both right after a freshly-verified OFFER and by
    `_step_waiting_provider()`'s re-check. `on_provider_unknown`, when
    given, is called (and its optional reply collected) only on the
    provider-still-unknown branch - used to send ACK_PROVIDER_UNKNOWN
    exactly once (criterion #16: "один проверенный status reply")."""

    provider = provider_registry.resolve(provider_id_text)
    if provider is None:
        replies: List[bytes] = []
        if on_provider_unknown is not None:
            ack = on_provider_unknown()
            if ack is not None:
                replies.append(ack)
        _set_state(conn, attachment_id, WAITING_PROVIDER, now)
        conn.commit()
        return ReceiveResult(attachment_id=attachment_id, state=WAITING_PROVIDER, replies=replies)
    new_state = WAITING_CONSENT if network_available else WAITING_NETWORK
    _set_state(conn, attachment_id, new_state, now)
    conn.commit()
    return ReceiveResult(attachment_id=attachment_id, state=new_state, replies=[])


# ---- entry point 1: handle an inbound OFFER --------------------------------


def handle_offer(
    conn: sqlite3.Connection,
    *,
    workspace_manager: MCAWorkspaceManager,
    principal: MCAPrincipal,
    provider_registry: ProviderRegistry,
    key_exchange: KeyExchangeCoordinator,
    raw_offer: bytes,
    network_available: bool,
    now: Optional[float] = None,
    source_address: Optional[str] = None,
) -> ReceiveResult:
    """Handle one inbound OFFER frame's already-`ingest()`-ed logical
    bytes. `key_exchange` is this workspace/adapter's own
    `KeyExchangeCoordinator` (already constructed by the caller, same
    instance driving KEY_REQUEST/KEY_ANNOUNCE) - used here only for its
    read-only `get_binding_by_key_id()` lookup, never mutated.

    `source_address` (PR #227 defect #1) is persisted on the new
    attachment row as `reply_route_type`/`reply_route_id` - the only
    place this module ever learns where a reply for this attachment
    should go, since receiver.py holds no transport of its own (module
    docstring) and never will. Optional and defaults to `None` for
    callers (mostly this module's own test suite) that only care about
    this function's state-machine/dedup behavior, not real dispatch: an
    attachment created with no route recorded simply never has its
    queued ACKs picked up by AttachmentsService's dispatch step (nothing
    to send them through), exactly as if this fix didn't exist for that
    one attachment - not a crash, not a wrong send target.

    A repeat OFFER for a `transfer_id` this workspace has already
    recorded is idempotent: if the existing attachment is still
    non-terminal, its current state is re-evaluated via `run_step()`
    (itself idempotent - no new ACK is sent if one was already sent, no
    new state transition happens if nothing changed); if it is already
    terminal, the existing state is returned unchanged with no replies
    and no work at all (ADR-0001 section 6's dedup rule).

    An OFFER whose signature fails verification (when a binding *is*
    already known for its `sender_key_id`) is rejected outright - no
    attachment row is created for it at all. This is the one path with no
    corresponding state: criterion #18 ("a tampered pointer signature is
    never shown as valid") applies here just as much as to the
    descriptor/chunk signatures checked later in `Verifying`.
    """

    now = _now() if now is None else now

    try:
        unverified = codec.decode_offer(raw_offer, verify_key=None)
    except codec.CodecError as exc:
        raise ReceiverError(f"not a well-formed OFFER: {exc}") from exc

    transfer_id_hex = unverified.transfer_id.hex()
    existing = _find_by_transfer_id(conn, principal.workspace_id, transfer_id_hex)
    if existing is not None:
        if is_terminal(existing["state"]):
            return ReceiveResult(attachment_id=existing["id"], state=existing["state"], replies=[])
        return run_step(
            conn,
            workspace_manager=workspace_manager,
            principal=principal,
            provider_registry=provider_registry,
            key_exchange=key_exchange,
            network_available=network_available,
            attachment_id=existing["id"],
            now=now,
        )

    sender_key_id_hex = unverified.sender_key_id.hex()
    binding = key_exchange.get_binding_by_key_id(sender_key_id_hex)

    if binding is not None:
        try:
            codec.decode_offer(raw_offer, verify_key=VerifyKey(binding.public_identity))
        except codec.CodecError as exc:
            raise ReceiverError(f"OFFER signature verification failed for known key: {exc}") from exc

    attachment_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, sender_principal_id, provider_id, state,
             created_at, hard_expires_at, download_grace_seconds, pending_offer_cbor,
             reply_route_type, reply_route_id)
        VALUES (?, ?, ?, 'received', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            principal.workspace_id,
            transfer_id_hex,
            principal.principal_id,
            (binding.principal_id if binding is not None else None),
            encode_provider_id(unverified.provider_id),
            OFFER_RECEIVED,
            now,
            unverified.hard_expires_at,
            DEFAULT_DOWNLOAD_GRACE_SECONDS,
            (raw_offer if binding is None else None),
            # "DIRECT" - must match delivery.base.RouteType.DIRECT.value.
            # Not imported here (this module never imports anything from
            # delivery.base/DeliveryAdapter, per its own module docstring)
            # since an OFFER-triggered reply is always a direct reply to
            # whoever sent the OFFER - there is no other route shape an
            # inbound OFFER could have arrived over. `None` (not "DIRECT")
            # when `source_address` itself is `None`, so the dispatch step
            # can tell "no route recorded" apart from "recorded, empty".
            ("DIRECT" if source_address is not None else None),
            source_address,
        ),
    )
    _record_event(
        conn, attachment_id, now, "offer_received", {"kind": unverified.kind, "size_bucket": unverified.size_bucket}
    )

    replies: List[bytes] = []
    ack = _maybe_send_ack(
        conn,
        attachment_id=attachment_id,
        transfer_id=unverified.transfer_id,
        message_type=_ACK_RECEIVED_TYPE,
        event_type=_EVENT_ACK_RECEIVED_SENT,
        principal=principal,
        workspace_manager=workspace_manager,
        now=now,
    )
    if ack is not None:
        replies.append(ack)
    conn.commit()

    if binding is None:
        _set_state(conn, attachment_id, WAITING_KEY, now)
        conn.commit()
        return ReceiveResult(attachment_id=attachment_id, state=WAITING_KEY, replies=replies)

    def _send_provider_unknown_ack() -> Optional[bytes]:
        return _maybe_send_ack(
            conn,
            attachment_id=attachment_id,
            transfer_id=unverified.transfer_id,
            message_type=_ACK_PROVIDER_UNKNOWN_TYPE,
            event_type=_EVENT_ACK_PROVIDER_UNKNOWN_SENT,
            principal=principal,
            workspace_manager=workspace_manager,
            now=now,
        )

    result = _resolve_provider_or_wait(
        conn,
        attachment_id=attachment_id,
        provider_id_text=encode_provider_id(unverified.provider_id),
        provider_registry=provider_registry,
        network_available=network_available,
        now=now,
        on_provider_unknown=_send_provider_unknown_ack,
    )
    return ReceiveResult(attachment_id=attachment_id, state=result.state, replies=replies + result.replies)


# ---- entry point 2: advance one attachment as far as automatic logic goes --


def run_step(
    conn: sqlite3.Connection,
    *,
    workspace_manager: MCAWorkspaceManager,
    principal: MCAPrincipal,
    provider_registry: ProviderRegistry,
    key_exchange: KeyExchangeCoordinator,
    network_available: bool,
    attachment_id: str,
    relay_client: Optional[RelayClient] = None,
    now: Optional[float] = None,
) -> ReceiveResult:
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    state = row["state"]

    if state == WAITING_KEY:
        return _step_waiting_key(
            conn, row, workspace_manager, principal, provider_registry, key_exchange, network_available, now
        )
    if state == WAITING_PROVIDER:
        return _step_waiting_provider(conn, row, workspace_manager, principal, provider_registry, network_available, now)
    if state == WAITING_NETWORK:
        return _step_waiting_network(conn, row, network_available, now)
    if state in (DOWNLOADING, VERIFYING):
        # Verifying is reached and completed synchronously inside
        # _step_downloading in this pass (no genuine chunk/verify resume -
        # ADR-0007 decision 6). If a caller ever observes VERIFYING as the
        # *current* row state (e.g. a crash strictly between the two),
        # redoing Downloading from scratch is safe and simplest.
        return _step_downloading(conn, row, workspace_manager, principal, provider_registry, relay_client, now)
    # OFFER_RECEIVED is only ever acted on inline by handle_offer() (it
    # already has the just-decoded OfferFields in hand). A stray
    # OFFER_RECEIVED row reaching run_step() directly (should not happen
    # in normal operation) is left as-is rather than guessed at.
    return ReceiveResult(attachment_id=attachment_id, state=state, replies=[])


def _build_provider_unknown_callback(conn, *, attachment_id, transfer_id, principal, workspace_manager, now):
    def _send() -> Optional[bytes]:
        return _maybe_send_ack(
            conn,
            attachment_id=attachment_id,
            transfer_id=transfer_id,
            message_type=_ACK_PROVIDER_UNKNOWN_TYPE,
            event_type=_EVENT_ACK_PROVIDER_UNKNOWN_SENT,
            principal=principal,
            workspace_manager=workspace_manager,
            now=now,
        )

    return _send


def _step_waiting_key(conn, row, workspace_manager, principal, provider_registry, key_exchange, network_available, now):
    attachment_id = row["id"]
    pending_raw = row["pending_offer_cbor"]
    if pending_raw is None:
        # Should not happen (handle_offer always stores it when entering
        # WAITING_KEY) - fail closed rather than guess at a signer.
        return ReceiveResult(attachment_id=attachment_id, state=WAITING_KEY, replies=[])

    unverified = codec.decode_offer(bytes(pending_raw), verify_key=None)
    binding = key_exchange.get_binding_by_key_id(unverified.sender_key_id.hex())
    if binding is None:
        return ReceiveResult(attachment_id=attachment_id, state=WAITING_KEY, replies=[])

    try:
        codec.decode_offer(bytes(pending_raw), verify_key=VerifyKey(binding.public_identity))
    except codec.CodecError:
        # A binding now exists for this sender_key_id, but it does not
        # validate the very OFFER we parked - treat as tampered/replayed
        # under a since-rotated key, never surfaced as a valid transfer.
        _set_state(conn, attachment_id, FAILED, now, error_code="pending_offer_signature_invalid")
        conn.execute("UPDATE attachments SET pending_offer_cbor = NULL WHERE id = ?", (attachment_id,))
        conn.commit()
        return ReceiveResult(attachment_id=attachment_id, state=FAILED, replies=[])

    conn.execute(
        "UPDATE attachments SET sender_principal_id = ?, pending_offer_cbor = NULL WHERE id = ?",
        (binding.principal_id, attachment_id),
    )
    return _resolve_provider_or_wait(
        conn,
        attachment_id=attachment_id,
        provider_id_text=encode_provider_id(unverified.provider_id),
        provider_registry=provider_registry,
        network_available=network_available,
        now=now,
        on_provider_unknown=_build_provider_unknown_callback(
            conn,
            attachment_id=attachment_id,
            transfer_id=unverified.transfer_id,
            principal=principal,
            workspace_manager=workspace_manager,
            now=now,
        ),
    )


def _step_waiting_provider(conn, row, workspace_manager, principal, provider_registry, network_available, now):
    attachment_id = row["id"]
    return _resolve_provider_or_wait(
        conn,
        attachment_id=attachment_id,
        provider_id_text=row["provider_id"],
        provider_registry=provider_registry,
        network_available=network_available,
        now=now,
        on_provider_unknown=_build_provider_unknown_callback(
            conn,
            attachment_id=attachment_id,
            transfer_id=bytes.fromhex(row["transfer_id"]),
            principal=principal,
            workspace_manager=workspace_manager,
            now=now,
        ),
    )


def _step_waiting_network(conn, row, network_available, now):
    attachment_id = row["id"]
    if not network_available:
        return ReceiveResult(attachment_id=attachment_id, state=WAITING_NETWORK, replies=[])
    _set_state(conn, attachment_id, WAITING_CONSENT, now)
    conn.commit()
    return ReceiveResult(attachment_id=attachment_id, state=WAITING_CONSENT, replies=[])


def begin_download(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    """The explicit user action ("Скачать") that moves WAITING_CONSENT ->
    DOWNLOADING. Never called automatically - design spec 13:
    "Автоматическая загрузка по умолчанию выключена."."""

    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if row["state"] != WAITING_CONSENT:
        raise ReceiverError(f"attachment {attachment_id!r} is {row['state']!r}, not WAITING_CONSENT - cannot begin download")
    _set_state(conn, attachment_id, DOWNLOADING, now)
    conn.commit()
    return DOWNLOADING


def reject(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if row["state"] != WAITING_CONSENT:
        raise ReceiverError(f"attachment {attachment_id!r} is {row['state']!r}, not WAITING_CONSENT - cannot reject")
    _set_state(conn, attachment_id, REJECTED, now)
    conn.commit()
    return REJECTED


# ---- Downloading -> Verifying -> Available/Failed --------------------------


def _quarantine(workspace_manager: MCAWorkspaceManager, principal_id: str, transfer_id_hex: str, data: bytes) -> None:
    paths = workspace_manager.ensure_workspace(principal_id)
    (paths.quarantine / f"{transfer_id_hex}.bin").write_bytes(data)


def _fail(conn, attachment_id, now, error_code) -> ReceiveResult:
    _set_state(conn, attachment_id, FAILED, now, error_code=error_code)
    conn.commit()
    return ReceiveResult(attachment_id=attachment_id, state=FAILED, replies=[])


def _wait_network(conn, attachment_id, now) -> ReceiveResult:
    _set_state(conn, attachment_id, WAITING_NETWORK, now)
    conn.commit()
    return ReceiveResult(attachment_id=attachment_id, state=WAITING_NETWORK, replies=[])


def _step_downloading(conn, row, workspace_manager, principal, provider_registry, relay_client, now):
    attachment_id = row["id"]
    transfer_id = bytes.fromhex(row["transfer_id"])
    transfer_id_hex = row["transfer_id"]

    provider = provider_registry.resolve(row["provider_id"])
    if provider is None:
        # Should not normally happen (WAITING_PROVIDER already gated
        # this), but fail closed rather than guessing an endpoint.
        return _fail(conn, attachment_id, now, "provider_missing")

    client = relay_client if relay_client is not None else RelayClient(provider.origin)

    try:
        descriptor = client.get_descriptor(transfer_id)
    except RelayUnavailableError:
        return _wait_network(conn, attachment_id, now)
    except RelayHTTPError as exc:
        return _fail(conn, attachment_id, now, f"relay_{exc.code}")

    try:
        verify_descriptor_signature(descriptor, provider.service_public_key)
    except RelayVerificationError:
        return _fail(conn, attachment_id, now, "descriptor_signature_invalid")

    chunk_count = len(descriptor.chunks)
    plain_size = sum(c.size for c in descriptor.chunks) - chunk_count * crypto.TAG_BYTES

    try:
        parsed = manifest.parse_manifest_blob(descriptor.encrypted_manifest)
        envelope = manifest.find_recipient_envelope(parsed, bytes.fromhex(principal.key_id))
        signing_key = identity.load_signing_key(workspace_manager, principal)
        secret = manifest.open_recipient_secret(envelope.sealed_envelope, signing_key)
        header = manifest.decrypt_manifest_header(
            parsed,
            data_key=secret.data_key,
            nonce_prefix=secret.nonce_prefix,
            chunk_count=chunk_count,
            plain_size=plain_size,
        )
    except manifest.ManifestError:
        _quarantine(workspace_manager, principal.principal_id, transfer_id_hex, descriptor.encrypted_manifest)
        return _fail(conn, attachment_id, now, "manifest_invalid")

    if header.chunk_count != chunk_count or header.plain_size != plain_size:
        return _fail(conn, attachment_id, now, "header_size_mismatch")

    assembled = bytearray()
    for index in range(chunk_count):
        try:
            ciphertext = client.get_chunk(transfer_id, index)
        except RelayUnavailableError:
            return _wait_network(conn, attachment_id, now)
        except RelayHTTPError:
            return _fail(conn, attachment_id, now, "chunk_fetch_failed")

        expected_chunk = descriptor.chunks[index]
        if hashlib.sha256(ciphertext).digest() != expected_chunk.sha256:
            _quarantine(workspace_manager, principal.principal_id, transfer_id_hex, ciphertext)
            return _fail(conn, attachment_id, now, "chunk_digest_mismatch")

        try:
            plaintext_chunk = crypto.decrypt_chunk(
                data_key=secret.data_key,
                nonce_prefix=secret.nonce_prefix,
                transfer_id=transfer_id,
                index=index,
                chunk_count=chunk_count,
                plain_size=plain_size,
                ciphertext=ciphertext,
            )
        except crypto.CryptoError:
            _quarantine(workspace_manager, principal.principal_id, transfer_id_hex, ciphertext)
            return _fail(conn, attachment_id, now, "chunk_aead_failed")

        assembled.extend(plaintext_chunk)

    if hashlib.sha256(bytes(assembled)).digest() != header.plain_sha256:
        _quarantine(workspace_manager, principal.principal_id, transfer_id_hex, bytes(assembled))
        return _fail(conn, attachment_id, now, "plaintext_digest_mismatch")

    saved_path = workspace_manager.unique_file_name(principal.principal_id, header.file_name)
    saved_path.write_bytes(bytes(assembled))

    try:
        client.complete(transfer_id, secret.receipt_secret)
    except RelayError:
        # The file is already safely saved locally - a failed receipt
        # call is a Relay bookkeeping/cleanup concern, not a reason to
        # treat a successfully verified and saved file as unavailable to
        # the user. `/complete` is documented idempotent, so this is safe
        # to simply not retry synchronously here.
        pass

    _set_state(
        conn,
        attachment_id,
        AVAILABLE,
        now,
        extra_sql=", file_name = ?, mime_type = ?, plain_size = ?, plain_sha256 = ?, saved_path = ?",
        extra_params=(header.file_name, header.mime_type, header.plain_size, header.plain_sha256.hex(), str(saved_path)),
    )
    ack = _maybe_send_ack(
        conn,
        attachment_id=attachment_id,
        transfer_id=transfer_id,
        message_type=_ACK_DOWNLOADED_TYPE,
        event_type=_EVENT_ACK_DOWNLOADED_SENT,
        principal=principal,
        workspace_manager=workspace_manager,
        now=now,
    )
    conn.commit()
    return ReceiveResult(attachment_id=attachment_id, state=AVAILABLE, replies=([ack] if ack else []))


# ---- reconciliation (mirrors sender.resume_pending()) ----------------------


def reconcile_pending(
    conn: sqlite3.Connection,
    *,
    workspace_manager: MCAWorkspaceManager,
    principal: MCAPrincipal,
    provider_registry: ProviderRegistry,
    key_exchange: KeyExchangeCoordinator,
    network_available: bool,
    now: Optional[float] = None,
) -> List[ReceiveResult]:
    """Re-evaluate every non-terminal received attachment currently in
    WAITING_KEY/WAITING_PROVIDER/WAITING_NETWORK, after an external event
    (a new binding was recorded, a provider was just registered, network
    came back). Never touches WAITING_CONSENT (explicit user action only)
    or a row already in DOWNLOADING - this is meant for the
    Step 1.5 DoD's reconciliation case, not as a general-purpose
    background download driver."""

    now = _now() if now is None else now
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id FROM attachments WHERE workspace_id = ? AND direction = 'received' AND state IN (?, ?, ?)",
        (principal.workspace_id, WAITING_KEY, WAITING_PROVIDER, WAITING_NETWORK),
    ).fetchall()
    results = []
    for row in rows:
        results.append(
            run_step(
                conn,
                workspace_manager=workspace_manager,
                principal=principal,
                provider_registry=provider_registry,
                key_exchange=key_exchange,
                network_available=network_available,
                attachment_id=row["id"],
                now=now,
            )
        )
    return results
