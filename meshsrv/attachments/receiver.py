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
from meshsrv.attachments.key_exchange import AddressStatus, KeyExchangeCoordinator
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
CANCELLED = "CANCELLED"
FAILED = "FAILED"

TERMINAL_STATES = frozenset({AVAILABLE, EXPIRED, REJECTED, CANCELLED, FAILED})
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

# PR #231 review, section 4.3: real, persisted ACK rate limiting -
# mca_ack_quota (migration 10). Two independent fixed windows: one per
# source_address (a single misbehaving/spoofed sender cannot exhaust the
# whole workspace's reply budget) and one global (a workspace-wide
# ceiling regardless of how many distinct addresses are involved).
# Conservative MVP starting points, named so Step 1.9's real Pi Zero 2 W
# + real LoRa airtime hardware pass can retune them with evidence -
# LoRa's own airtime duty-cycle limits are the real ceiling this exists
# to respect, and this project has not yet measured that directly (same
# open-uncertainty framing as RELAY_HEALTH_INTERVAL_SECONDS in
# connectivity_monitor.py).
ACK_PER_SOURCE_LIMIT = 5
ACK_PER_SOURCE_WINDOW_SECONDS = 600  # 10 minutes - same window shape as key_exchange.py's own per-address gate
ACK_GLOBAL_LIMIT = 30
ACK_GLOBAL_WINDOW_SECONDS = 3600  # 1 hour
_ACK_QUOTA_GLOBAL_SCOPE = "__global__"
# Bounds how many stale per-source quota rows one check_and_record_reply_
# quota() call prunes - keeps mca_ack_quota's size from growing without
# bound over a long-running instance's lifetime (many distinct sender
# addresses over time) without ever doing an unbounded table scan.
_ACK_QUOTA_CLEANUP_BATCH = 50
_ACK_QUOTA_STALE_AFTER_SECONDS = 86400  # a full day past its own window - safely expired either way

# PR #231 review (2nd pass): inbound OFFER admission limits - a real gap
# the earlier ACK-outbox rate limiting above did not cover. Nothing
# previously bounded how many `attachments` rows a flood of OFFERs
# carrying distinct, attacker-chosen `transfer_id`s could create (the
# existing transfer_id dedup in handle_offer() only protects against a
# *repeated* transfer_id, not against unlimited *distinct* ones) - each
# accepted OFFER is a permanent DB row until it resolves or expires, so
# this was an unbounded-storage-growth admission gap, not just a rate
# concern. Same two-tier shape as ACK_PER_SOURCE_LIMIT/ACK_GLOBAL_LIMIT
# above (a single misbehaving/spoofed sender cannot exhaust the whole
# workspace's admission budget on its own), but counting *live*
# (non-terminal) 'received' rows rather than a rolling time window -
# this is a capacity ceiling, not a send-rate limit. Conservative MVP
# starting points, same "named constant for Step 1.9's real hardware
# pass to retune" framing as the ACK quota above.
MAX_PENDING_RECEIVED_PER_SOURCE = 20
MAX_PENDING_RECEIVED_GLOBAL = 200


@dataclasses.dataclass(frozen=True)
class ReplyRoute:
    """Everything `_dispatch_outgoing_replies()` needs to reconstruct the
    same transport destination after a restart, persisted once at
    `handle_offer()` time (PR #231 review, section 4.2) - deliberately a
    small immutable bundle instead of a bare `source_address` string, so
    a future non-Meshtastic adapter does not have to be threaded through
    as yet another loose positional parameter. `destination_address` is
    the address `Route(...)` needs when actually sending; for the
    DIRECT-only MVP this is always the same value as `route_id` (an
    inbound OFFER's own sender), but the two are kept as separate fields
    - not silently assumed equal - so a future CHANNEL/non-DIRECT route
    (where sending back is not simply "reply to the sender") does not
    have to change this shape, only stop conflating them."""

    adapter_id: str
    connector_profile_id: str
    route_type: str
    route_id: str
    destination_address: str


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
    """One row due for a real send attempt. `route_type`/`route_id`/
    `adapter_id`/`connector_profile_id`/`destination_address` all come
    from the owning attachment's own `reply_route_type`/`reply_route_id`/
    `reply_adapter_id`/`reply_connector_profile_id`/
    `reply_destination_address` (set once, at `handle_offer()` time, from
    the real `DeliveryEnvelope` that received the OFFER - PR #231
    review, section 4.2). `route_type`/`route_id` being `None` means no
    route was ever recorded for this attachment (a caller that ran
    `handle_offer()` without `source_address`/`reply_route`, e.g. this
    module's own non-integration tests), which the dispatch step treats
    as permanently undeliverable rather than retrying forever for no
    reason. `adapter_id`/`connector_profile_id` can independently be
    `None` even when a route exists - the pre-hardening-pass
    `source_address`-only call shape (still accepted by `handle_offer()`
    for backward compatibility with callers that only need rate-
    limiting/audit) never recorded them. PR #231 review (3rd pass): the
    dispatch step now fails closed - permanently undeliverable, never
    guessed past - on either a *missing* `adapter_id`/
    `connector_profile_id` or a *mismatched* one against the dispatching
    service's own adapter. An earlier pass of this fix (2nd pass)
    treated a missing `adapter_id` as "unknown, trust the currently-
    configured adapter" - inconsistent with the fail-closed posture
    applied everywhere else in this review (TOFU binding, inbound-OFFER
    admission); not knowing which adapter/connector a reply belongs to
    is exactly the situation where guessing must not happen. In
    production this never matters: `AttachmentsService.
    _process_inbound_offer()` always builds a full `ReplyRoute` from the
    real `DeliveryEnvelope` that received the OFFER."""

    id: str
    attachment_id: str
    event_type: str
    message: bytes
    attempts: int
    route_type: Optional[str]
    route_id: Optional[str]
    adapter_id: Optional[str]
    connector_profile_id: Optional[str]
    destination_address: Optional[str]


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
               r.attempts AS attempts, a.reply_route_type AS route_type, a.reply_route_id AS route_id,
               a.reply_adapter_id AS adapter_id, a.reply_connector_profile_id AS connector_profile_id,
               a.reply_destination_address AS destination_address
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
            adapter_id=row["adapter_id"], connector_profile_id=row["connector_profile_id"],
            destination_address=row["destination_address"],
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


def _quota_row(conn: sqlite3.Connection, workspace_id: str, scope: str) -> Optional[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(
        "SELECT window_start_at, count FROM mca_ack_quota WHERE workspace_id = ? AND scope = ?",
        (workspace_id, scope),
    ).fetchone()


def _quota_would_exceed(row: Optional[sqlite3.Row], now: float, window_seconds: int, limit: int) -> bool:
    if row is None:
        return False
    if now - row["window_start_at"] >= window_seconds:
        return False  # window has rolled over - about to be reset, not exceeded
    return row["count"] >= limit


def _quota_bump(conn: sqlite3.Connection, workspace_id: str, scope: str, now: float, window_seconds: int) -> None:
    row = _quota_row(conn, workspace_id, scope)
    if row is None or now - row["window_start_at"] >= window_seconds:
        conn.execute(
            """
            INSERT INTO mca_ack_quota (workspace_id, scope, window_start_at, count) VALUES (?, ?, ?, 1)
            ON CONFLICT(workspace_id, scope) DO UPDATE SET window_start_at = excluded.window_start_at, count = 1
            """,
            (workspace_id, scope, now),
        )
    else:
        conn.execute(
            "UPDATE mca_ack_quota SET count = count + 1 WHERE workspace_id = ? AND scope = ?",
            (workspace_id, scope),
        )


def check_and_record_reply_quota(conn: sqlite3.Connection, workspace_id: str, source_address: str, now: float) -> bool:
    """PR #231 review, section 4.3: the real, persisted rate limit
    `_dispatch_outgoing_replies()` (service.py) checks before every send
    attempt - not just `MAX_OUTGOING_REPLY_SENDS_PER_DISPATCH` (which only
    ever bounded one SQL query's row count, not the actual send rate: at
    the default 5s tick interval that alone still allowed up to 60
    sends/minute with zero per-source protection). Checks (never records
    a partial success) both windows before bumping either - a reply that
    would exceed either the per-source or the global quota is rejected
    outright, with neither counter incremented, so a caller can safely
    retry the exact same call later without having already "spent" a slot
    it never actually got to use. Deliberately does not distinguish
    "which" quota was hit in its return value - both are visible via
    `mca_ack_quota` directly (e.g. for a future admin/debug view) if that
    ever matters; the caller's only decision is retry-later either way.

    Also does the small, bounded cleanup of stale per-source rows the
    review asks for (`_ACK_QUOTA_CLEANUP_BATCH` per call) - a source
    address that stops sending eventually has its quota row deleted
    rather than kept forever, without ever doing an unbounded scan."""
    conn.execute(
        """
        DELETE FROM mca_ack_quota WHERE rowid IN (
            SELECT rowid FROM mca_ack_quota
            WHERE workspace_id = ? AND scope != ? AND ? - window_start_at > ?
            LIMIT ?
        )
        """,
        (workspace_id, _ACK_QUOTA_GLOBAL_SCOPE, now, _ACK_QUOTA_STALE_AFTER_SECONDS, _ACK_QUOTA_CLEANUP_BATCH),
    )

    per_source_row = _quota_row(conn, workspace_id, source_address)
    global_row = _quota_row(conn, workspace_id, _ACK_QUOTA_GLOBAL_SCOPE)
    if _quota_would_exceed(per_source_row, now, ACK_PER_SOURCE_WINDOW_SECONDS, ACK_PER_SOURCE_LIMIT):
        conn.commit()
        return False
    if _quota_would_exceed(global_row, now, ACK_GLOBAL_WINDOW_SECONDS, ACK_GLOBAL_LIMIT):
        conn.commit()
        return False

    _quota_bump(conn, workspace_id, source_address, now, ACK_PER_SOURCE_WINDOW_SECONDS)
    _quota_bump(conn, workspace_id, _ACK_QUOTA_GLOBAL_SCOPE, now, ACK_GLOBAL_WINDOW_SECONDS)
    conn.commit()
    return True


def _would_exceed_inbound_admission_limit(
    conn: sqlite3.Connection, workspace_id: str, source_address: Optional[str]
) -> Optional[str]:
    """PR #231 review (2nd pass): the admission-control check
    `handle_offer()` runs before creating a new attachment row for a
    brand-new (never-before-seen) `transfer_id` - the existing dedup in
    `handle_offer()` only protects against a *repeated* transfer_id, not
    against a flood of distinct, attacker-chosen ones, each of which
    would otherwise become a permanent DB row. Returns an error code
    string if admission should be refused, `None` if there is room.

    Counts *live* (non-terminal) 'received' rows - a resolved/expired/
    rejected/failed attachment stops counting against either limit,
    matching this being a capacity ceiling on outstanding work, not a
    historical audit constraint. The per-source count only applies when
    `source_address` is known (the source_address-only/no-route callers
    this module already treats as a supported, simpler shape elsewhere -
    PendingReply's own docstring) - such a row still counts toward the
    global ceiling regardless."""
    terminal_placeholders = ",".join("?" for _ in TERMINAL_STATES)
    global_count = conn.execute(
        f"""
        SELECT COUNT(*) FROM attachments
        WHERE workspace_id = ? AND direction = 'received' AND state NOT IN ({terminal_placeholders})
        """,
        (workspace_id, *TERMINAL_STATES),
    ).fetchone()[0]
    if global_count >= MAX_PENDING_RECEIVED_GLOBAL:
        return f"pending_received_global_limit_exceeded:{global_count}"

    if source_address is not None:
        per_source_count = conn.execute(
            f"""
            SELECT COUNT(*) FROM attachments
            WHERE workspace_id = ? AND direction = 'received' AND reply_route_id = ?
                AND state NOT IN ({terminal_placeholders})
            """,
            (workspace_id, source_address, *TERMINAL_STATES),
        ).fetchone()[0]
        if per_source_count >= MAX_PENDING_RECEIVED_PER_SOURCE:
            return f"pending_received_per_source_limit_exceeded:{per_source_count}"

    return None


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
    reply_route: Optional[ReplyRoute] = None,
) -> ReceiveResult:
    """Handle one inbound OFFER frame's already-`ingest()`-ed logical
    bytes. `key_exchange` is this workspace/adapter's own
    `KeyExchangeCoordinator` (already constructed by the caller, same
    instance driving KEY_REQUEST/KEY_ANNOUNCE) - used here only for its
    read-only `get_binding_by_key_id()` lookup, never mutated.

    `reply_route` (PR #231 review, section 4.2 - supersedes the bare
    `source_address`-only version from PR #227 defect #1) is persisted
    on the new attachment row (`reply_adapter_id`/
    `reply_connector_profile_id`/`reply_route_type`/`reply_route_id`/
    `reply_destination_address`) - the only place this module ever
    learns where a reply for this attachment should go, since
    receiver.py holds no transport of its own (module docstring) and
    never will. `source_address` alone is kept as a separate, simpler
    parameter for callers (mostly this module's own test suite, and any
    caller that only cares about rate-limiting/audit, not real dispatch)
    that do not need a full reply route - when only `source_address` is
    given, `reply_route` is left unset and this attachment's queued ACKs
    are never picked up by AttachmentsService's dispatch step (nothing
    to send them through), exactly as before this fix - not a crash,
    not a wrong send target. When both are given, `reply_route`'s own
    `route_id` must equal `source_address` (DIRECT-only MVP invariant -
    see `ReplyRoute`'s own docstring for why the two are still kept as
    separate fields rather than merged into one).

    A repeat OFFER for a `transfer_id` this workspace has already
    recorded is idempotent: if the existing attachment is still
    non-terminal, its current state is re-evaluated via `run_step()`
    (itself idempotent - no new ACK is sent if one was already sent, no
    new state transition happens if nothing changed); if it is already
    terminal, the existing state is returned unchanged with no replies
    and no work at all (ADR-0001 section 6's dedup rule).

    An OFFER whose signature fails verification (when a binding is already
    known **and `MCA_READY`** for its `sender_key_id`) is rejected outright -
    no attachment row is created for it at all. This is the one path with no
    corresponding state: criterion #18 ("a tampered pointer signature is
    never shown as valid") applies here just as much as to the
    descriptor/chunk signatures checked later in `Verifying`. A binding that
    is known but not `MCA_READY` (`KEY_UNVERIFIED`/`KEY_CHANGED`) is NOT
    verified here - it is parked in `WAITING_KEY` like an unknown key, and
    the signature is checked on resume in `_step_waiting_key` once the human
    has explicitly trusted (or resolved) the key.
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

    # PR 1 (trust gate at admission): a binding must be MCA_READY (TOFU-
    # confirmed, no pending rotation) before its key may authorize an OFFER.
    # KEY_UNVERIFIED (announced, never confirmed) and KEY_CHANGED (a
    # conflicting key pending accept/reject) are treated exactly like an
    # unknown key: the OFFER is parked in WAITING_KEY and signature-checked
    # only once the key is explicitly trusted (see _step_waiting_key). An
    # unverified or not-yet-accepted key never authorizes a file here.
    binding_ready = binding is not None and binding.status == AddressStatus.MCA_READY

    if binding_ready:
        try:
            codec.decode_offer(raw_offer, verify_key=VerifyKey(binding.public_identity))
        except codec.CodecError as exc:
            raise ReceiverError(f"OFFER signature verification failed for known key: {exc}") from exc

    if reply_route is not None and source_address is not None and reply_route.route_id != source_address:
        raise ReceiverError(
            f"reply_route.route_id ({reply_route.route_id!r}) must match source_address ({source_address!r})"
        )
    effective_source_address = source_address if source_address is not None else (
        reply_route.route_id if reply_route is not None else None
    )

    admission_rejection = _would_exceed_inbound_admission_limit(
        conn, principal.workspace_id, effective_source_address
    )
    if admission_rejection is not None:
        raise ReceiverError(f"OFFER rejected: {admission_rejection}")

    attachment_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO attachments
            (id, workspace_id, transfer_id, direction, principal_id, sender_principal_id, provider_id, state,
             created_at, hard_expires_at, download_grace_seconds, pending_offer_cbor,
             reply_route_type, reply_route_id,
             reply_adapter_id, reply_connector_profile_id, reply_destination_address)
        VALUES (?, ?, ?, 'received', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment_id,
            principal.workspace_id,
            transfer_id_hex,
            principal.principal_id,
            (binding.principal_id if binding_ready else None),
            encode_provider_id(unverified.provider_id),
            OFFER_RECEIVED,
            now,
            unverified.hard_expires_at,
            DEFAULT_DOWNLOAD_GRACE_SECONDS,
            (raw_offer if not binding_ready else None),
            # "DIRECT" - must match delivery.base.RouteType.DIRECT.value.
            # Not imported here (this module never imports anything from
            # delivery.base/DeliveryAdapter, per its own module docstring)
            # since an OFFER-triggered reply is always a direct reply to
            # whoever sent the OFFER - there is no other route shape an
            # inbound OFFER could have arrived over. `None` (not "DIRECT")
            # when no address is known at all, so the dispatch step can
            # tell "no route recorded" apart from "recorded, empty".
            ("DIRECT" if effective_source_address is not None else None),
            effective_source_address,
            (reply_route.adapter_id if reply_route is not None else None),
            (reply_route.connector_profile_id if reply_route is not None else None),
            (reply_route.destination_address if reply_route is not None else effective_source_address),
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

    if not binding_ready:
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
    if binding is None or binding.status != AddressStatus.MCA_READY:
        # PR 1 (recoverable missing-key workflow): a parked transfer resumes
        # ONLY once its signer's key is explicitly trusted. `binding is None`
        # means the key is still unknown; `binding.status != MCA_READY` covers
        # both KEY_UNVERIFIED (a KEY_ANNOUNCE arrived but no human confirmed
        # it) and KEY_CHANGED (a conflicting key is pending accept/reject).
        # Advancing on either would let an unverified key authorize a file -
        # exactly the trust boundary TOFU exists to hold. `get_binding_by_key_id`
        # is deliberately address-agnostic and does NOT filter on trust, so
        # that filter has to live here, at the state-transition boundary.
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


def apply_cancelled(conn: sqlite3.Connection, attachment_id: str, now: Optional[float] = None) -> str:
    """Inbound CANCEL (ADR-0001): the sender withdrew this offer. Any
    non-terminal state (OFFER_RECEIVED .. VERIFYING) -> CANCELLED (terminal);
    any terminal state -> no-op (a stale/duplicate CANCEL never regresses an
    AVAILABLE/EXPIRED/REJECTED/FAILED row, and never overwrites a different
    terminal outcome). The caller has already verified the CANCEL's signature
    and sender identity, so this is the pure state write only."""
    now = _now() if now is None else now
    row = _row(conn, attachment_id)
    if is_terminal(row["state"]):
        return row["state"]
    _set_state(conn, attachment_id, CANCELLED, now)
    _record_event(conn, attachment_id, now, "cancelled", {"to": CANCELLED})
    conn.commit()
    return CANCELLED


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

    # Step 1.6A.5 storage model: the verified plaintext is written to the
    # internal content cache under its canonical attachment id - never the
    # sender-controlled `header.file_name`, which is retained only as the
    # safe *display* name in `file_name`. The descriptor still points into
    # `cache/incoming/` (content_available=true, saved=false) until the user
    # explicitly saves, which is what moves it into `files/`.
    paths = workspace_manager.paths(principal.principal_id)
    cache_path = paths.cache_incoming / attachment_id
    cache_path.write_bytes(bytes(assembled))

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
        extra_params=(header.file_name, header.mime_type, header.plain_size, header.plain_sha256.hex(), str(cache_path)),
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
