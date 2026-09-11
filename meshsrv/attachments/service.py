"""meshsrv/attachments/service.py

ADR-0008 decision 1 (Step 1.6A backend layer): AttachmentsService, the
single background worker that drives every non-terminal attachment
forward - this is what api_attachments.py's handlers will call wake()
into, once Step 1.6A's REST layer exists. This module deliberately does
NOT introduce a job queue on top of the existing (and, per ADR-0008's own
audit, never written-to) `mca_jobs` table: each tick is a direct SQL scan
of `attachments` for the rows sender.py's/receiver.py's own
AUTOMATIC_STATES sets say can be advanced without waiting for an external
event, followed by one sender.run_step()/receiver.run_step() call per row
- the tick body *is* the reconciliation pass, just run repeatedly.

One instance per workspace, constructed once at server startup alongside
ProviderRegistry/KeyExchangeCoordinator/ConnectivityMonitor (the same
"long-lived instance held for the process's life" shape used throughout
this codebase). A single daemon thread per workspace processes
attachments strictly serially inside one `threading.Lock`-held tick -
MeshCenter is single-process (ADR-0003), so no SQLite lease is needed,
and the design spec's "at most one crypto worker"/"at most two concurrent
transfers" requirements are satisfied trivially, since the real
concurrency ceiling here is 1 by construction (no thread pool).

API handlers must never call run_step() or touch the Relay/radio
themselves (ADR-0008): they validate input, make a cheap synchronous
domain-layer call (create_draft(), begin_download(), reject(), cancel()
...), call wake(), and return 202 Accepted. All the slow encrypt/upload/
download/decrypt/verify work happens on this module's own thread, never
on a Flask request thread.

`attachments.provider_id` is stored as Base64URL text for both
directions (the form `ProviderRegistry` keys rows by) - 'received' rows
always were (receiver.py, ADR-0007); 'sent' rows used to be stored as hex
instead (a reviewer-found defect: `ProviderRegistry.remove_or_disable()`'s
"is this provider still referenced?" check compares against the
Base64URL form, so it never matched a 'sent' row and could delete a
profile a real outgoing attachment still depended on - reproduced
locally). Fixed at the source in `sender.py` (ADR-0008-hardening), with
Migration 9 re-encoding any row a pre-fix process already wrote as hex.
`_provider_id_text()` below is now a thin, defensive pass-through kept
for the two call sites' clarity - see its own docstring for why it isn't
simply inlined.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import requests
from nacl.signing import VerifyKey

from meshsrv.attachments import codec, contacts, crypto, receiver, sender
from meshsrv.attachments.command_registry import CommandRegistry
from meshsrv.attachments.commands import MAX_COMMANDS_PER_TICK, Command, CommandQueue
from meshsrv.attachments.db.tombstones import is_tombstoned
from meshsrv.attachments.delivery.base import DeliveryAdapter, DeliveryEnvelope, DeliveryError, Route, RouteType
from meshsrv.attachments.dispatch import COMMAND_EXECUTION_FAILED, CommandDispatcher, CommandOutcome
from meshsrv.attachments.identity import MCAPrincipal, compute_key_id
from meshsrv.attachments.idempotency import PendingReservation, PendingReservations, validate_canonical_hash, validate_client_request_id
from meshsrv.attachments.key_exchange import AddressStatus, KeyExchangeCoordinator, RateLimited
from meshsrv.attachments.probe_registry import (
    PROBE_STATUS_PROBED,
    PROBE_TTL_SECONDS,
    ProbeRecord,
    ProbeRegistry,
    mint_probe_id,
    serialize_probe_record,
)
from meshsrv.attachments.provider_registry import (
    ProviderRegistry,
    ProviderRegistryError,
    compute_provider_id,
    decode_provider_id,
    encode_provider_id,
    normalize_origin,
)
from meshsrv.attachments.recipient_snapshot import RecipientSnapshotPublisher
from meshsrv.attachments.mime_allowlist import MAX_SOURCE_NAME_CODE_POINTS, sanitize_display_name
from meshsrv.attachments.relay_client import RelayClient, RelayError, RelayHTTPError
from meshsrv.attachments.relay_http import RelayNetworkError
from meshsrv.attachments.snapshots import (
    AttachmentsSnapshotPublisher,
    ContentLocatorError,
    _is_inside_files,
    make_locator,
    resolve_locator,
)
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor

logger = logging.getLogger(__name__)

# Periodic safety-net cadence (module docstring: wake() is the normal
# trigger: a new draft, an inbound message, a user action, a
# ConnectivityMonitor transition - this is only the fallback for
# anything that didn't call wake(), e.g. a retry_at deadline elapsing).
DEFAULT_TICK_SECONDS = 5.0

# Per ADR-0008: a config constant, not a hard architectural limit - the
# real concurrency ceiling is 1 regardless (attachments are processed
# strictly serially within one locked tick), this only bounds how much
# work one tick takes before yielding back to the wake-driven loop.
MAX_ATTACHMENTS_PER_TICK = 8

# PR 1 (recoverable missing-key workflow): bound how many KEY_REQUESTs one
# tick's automatic missing-key scan may *send*. A stranger who floods OFFERs
# from many distinct unknown-key addresses (bounded globally by
# receiver.MAX_PENDING_RECEIVED_GLOBAL) could otherwise trigger a like-sized
# burst of outbound radio sends in a single tick; this caps that burst the
# same way MAX_ATTACHMENTS_PER_TICK caps row work. Per-address rate limiting
# (migration 13) still governs how often any one address is asked.
MAX_AUTO_KEY_REQUESTS_PER_TICK = 8

# Internal tri-state results of `_auto_request_key_for_row` (PR 1 correction
# pass): distinguish "sent" (counts toward the per-tick cap) from "the
# workspace-wide hourly budget is exhausted" (stop the scan) from an ordinary
# skip (continue to the next row). Strings, not an enum, to match the module's
# existing lightweight-result style.
_AUTO_KEY_REQUEST_SENT = "sent"
_AUTO_KEY_REQUEST_QUOTA_EXHAUSTED = "quota_exhausted"

# PR #231 review, section 2 (single-owner SQLite model): the radio
# listener thread never touches the MCA database directly any more - it
# only builds an immutable InboundEvent and puts it on this bounded
# queue (see enqueue_inbound()'s own docstring). Bounded so a listener
# thread that outruns a stalled worker (e.g. every registered Relay
# timing out) cannot grow memory without limit; a full queue drops the
# oldest-pending event's *replacement* (put_nowait() raises queue.Full,
# the event is logged and discarded, never blocking the caller) rather
# than ever blocking the radio listener thread.
INBOUND_QUEUE_MAXSIZE = 256

# How many queued inbound events one tick drains before moving on to the
# rest of its work - bounds one tick's own duration under a flood the
# same way MAX_ATTACHMENTS_PER_TICK already bounds the row-scan; the
# rest simply wait for the next tick/wake(), never blocking anything.
MAX_INBOUND_EVENTS_PER_TICK = 16

# Finding 3: a bounded retry backlog for staged spool files whose removal
# failed on the worker. When a create does not commit a row that references
# its staged file (invalid payload, recipient failure, duplicate, conflict,
# or an exception before commit), the worker must not report success while
# the newly-unused plaintext still exists on disk. The id is retained here
# (bounded) and re-attempted a few per tick, rather than either blocking the
# tick or letting an unbounded in-memory set grow. Any file that survives
# this backlog (e.g. the process restarted) is reclaimed by Finding 5's
# bounded orphan-staging recovery.
SPOOL_CLEANUP_BACKLOG_MAXSIZE = 256

# How many backlogged spool-cleanup attempts one tick makes before moving on.
MAX_SPOOL_CLEANUP_PER_TICK = 8

# ADR-0009 Decision 5: how many expired `mca_sender_revoke_state` rows one tick
# deletes (bounded, so a large backlog drains over successive ticks rather than
# blocking one). The row protects a Relay object already past hard-expiry +
# download-grace, so its token has no remaining purpose.
MAX_REVOKE_STATE_CLEANUP_PER_TICK = 100

# PR 2.5 (outbound EXPIRED): how many received attachments one tick's expiry
# reconciliation pass may transition to EXPIRED (each enqueues one signed
# EXPIRED). Bounded so a large backlog of past-deadline offers drains over
# successive ticks rather than flushing a matching burst of outbound frames
# onto the radio all at once - the same per-tick work discipline as
# MAX_ATTACHMENTS_PER_TICK / MAX_OUTGOING_REPLY_SENDS_PER_DISPATCH.
MAX_RECEIVER_EXPIRY_PER_TICK = 8

# PR 2.5 correction: the *fixed* sanitized dispatch error tokens written to
# `mca_outgoing_replies.last_error` and logged by `_dispatch_outgoing_replies`.
# A persisted/logged error must never embed a route id, destination address,
# adapter id, connector profile id, or exception text - only one of these
# allowlisted tokens, plus (in logs only) a safe identifier and the exception
# class name. See `_dispatch_outgoing_replies()` for where each is produced.
REPLY_ROUTE_MISSING = "reply_route_missing"
REPLY_ROUTE_NOT_DIRECT = "reply_route_not_direct"
REPLY_ROUTE_INVALID_CONTACT = "reply_route_invalid_contact"
REPLY_DESTINATION_MISMATCH = "reply_destination_mismatch"
REPLY_ADAPTER_MISMATCH = "reply_adapter_mismatch"
REPLY_CONNECTOR_MISMATCH = "reply_connector_mismatch"
DELIVERY_ERROR = "delivery_error"
DELIVERY_RECEIPT_NOT_SENT = "delivery_receipt_not_sent"

# ADR-0009 Decision 7 (extended by ADR-0010): the inbound simple-ACK-shaped
# types routed to the signed-ACK path (`_process_inbound_ack`). ACK_RECEIVED /
# ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN / REJECTED / EXPIRED all verify and
# apply against a *sent* attachment (direction='sent'); CANCEL is the one
# sender-signed simple-ack that applies against a *received* attachment and is
# dispatched to `_process_inbound_cancel` instead (see `_process_one_inbound_event`).
# Every other message type continues its existing path (OFFER, key-exchange).
_INBOUND_ACK_TYPES = frozenset(
    {
        codec.MessageType.ACK_RECEIVED,
        codec.MessageType.ACK_DOWNLOADED,
        codec.MessageType.ACK_PROVIDER_UNKNOWN,
        codec.MessageType.REJECTED,
        codec.MessageType.EXPIRED,
    }
)

# Finding 5: bounded orphan-staging recovery. A staged file older than this
# many seconds is treated as abandoned - the request thread crashed (or the
# process died) between staging and enqueue/commit - and is eligible for
# deletion. Conservative: an active request's staged file is at most a few
# seconds old, so the threshold can never mistake it for an orphan.
ORPHAN_SPOOL_MIN_AGE_SECONDS = 3600.0

# How many spool-directory entries one tick examines, and how many files one
# tick actually deletes, before yielding back to the wake-driven loop - the
# same bounded-work-per-tick discipline as MAX_ATTACHMENTS_PER_TICK.
MAX_ORPHAN_SCAN_PER_TICK = 100
MAX_ORPHAN_DELETE_PER_TICK = 20

# Finding 5, cadence gate: the orphan sweep runs on the first tick after
# startup (so a fresh process reclaims the previous process's crash orphans),
# then at most once per this many seconds. The sweep is O(N) over the spool
# directory, so gating it keeps the steady-state tick cheap on a Pi Zero 2 W
# rather than rescanning the whole directory every wake-driven tick.
ORPHAN_RECOVERY_CADENCE_SECONDS = 300.0

# Finding 4 stages a temp file as a dot-prefixed, `.tmp`-suffixed name
# (`api/api_attachments.py` `_TEMP_SUFFIX`), distinct from a committed spool
# file (a bare 32-hex attachment_id). The recovery only ever deletes entries
# matching one of these two known conventions - never anything else.
_TEMP_SPOOL_SUFFIX = ".tmp"

# Attachment/command ids are uuid4().hex: exactly 32 lowercase hex chars.
# Anchored with \Z (not $) so a trailing newline cannot sneak through.
_HEX32_RE = re.compile(r"[0-9a-f]{32}\Z")

# Stage 1 contact id (Step 1.6A.3C §7.10): the canonical Meshtastic
# transport address - a `!` prefix followed by exactly 8 lowercase hex chars
# (e.g. `!756f9960`). Anchored with \Z so a trailing newline cannot sneak
# through; used by both the request thread's route validation and the
# worker's defensive re-validation.
_CONTACT_ID_RE = re.compile(r"![0-9a-f]{8}\Z")


def _is_contact_id(value) -> bool:
    """True iff `value` is a Stage 1 contact transport address (`!` + 8
    lowercase hex). Pure - no conn, no filesystem, no lock."""
    return isinstance(value, str) and _CONTACT_ID_RE.match(value) is not None

RelayClientFactory = Callable[[str], Optional[RelayClient]]


@dataclasses.dataclass(frozen=True)
class InboundEvent:
    """One raw incoming MCA1-TEXT message, captured by the radio listener
    thread (meshsrv.attachments.mca_runtime.handle_incoming_meshtastic_text())
    *before* any CBOR decoding, signature verification, or database
    access - all of that now happens only on AttachmentsService's own
    worker thread, once this event is drained off the queue (PR #231
    review, section 2). `text` is the raw MCA1-TEXT string exactly as
    received; `adapter.ingest()` (base64url decode + CBOR parse) runs on
    the worker side, not here."""

    text: str
    source_address: str
    packet_id: Optional[str]
    received_at: float


class AttachmentsServiceError(RuntimeError):
    """Base class for this module's errors."""


def _classify_spool_name(name: str) -> Optional[str]:
    """Finding 5: classify one `spool/outgoing/` entry name into the two known
    conventions the create endpoint writes (Finding 4) - `"committed"` for a
    bare 32-hex `attachment_id`, `"temp"` for a dot-prefixed `.tmp`-suffixed
    staging file - or `None` for anything else, which the recovery never
    deletes. Pure, and deliberately conservative: an unexpected name (a stray
    file, a subdirectory name, or anything a future stage might introduce)
    is left untouched rather than guessed at."""
    if _HEX32_RE.match(name):
        return "committed"
    if name.startswith(".") and name.endswith(_TEMP_SPOOL_SUFFIX):
        return "temp"
    return None


def _provider_id_text(direction: str, provider_id_column: Optional[str]) -> Optional[str]:
    """Both directions store the same Base64URL encoding on disk now
    (module docstring) - this is a thin pass-through, not a real
    normalization step, kept only so both call sites below read the same
    way regardless of direction and so a future re-introduction of a
    per-direction difference has one obvious place to fix instead of two
    call sites drifting apart again. `direction` is accepted but
    currently unused - a defensive signature, not dead weight: a caller
    passing the wrong direction for a row would be a bug worth being able
    to assert on later, not something to silently ignore. Returns None
    unchanged: a draft can reach VALIDATING/ENCRYPTING before a relay
    lookup is ever needed."""
    return provider_id_column or None


def _bounded_text(value, *, max_len: int, allow_empty: bool) -> Optional[str]:
    """Finding 3: a bounded, NUL-free string. Returns the string unchanged, or
    None if it is not a `str`, exceeds `max_len`, is empty when
    `allow_empty=False`, or contains an embedded NUL (which would truncate as a
    C string in some downstream consumers)."""
    if not isinstance(value, str):
        return None
    if len(value) > max_len:
        return None
    if not allow_empty and not value:
        return None
    if "\x00" in value:
        return None
    return value


@dataclasses.dataclass(frozen=True)
class _CommittedHandoff:
    """A committed create awaiting publish-before-release (Finding 1,
    Correction 3): the three values that must all appear in the published
    snapshot's idempotency index before the pending reservation is released.
    Tracking all three (not just `client_request_id`) is what lets the cleanup
    distinguish "the committed entry is visible" from "some entry for this id
    is visible" - a duplicate promote that has not yet been reflected in the
    snapshot must not release the reservation early."""

    client_request_id: str
    attachment_id: str
    canonical_hash: str


class AttachmentsService:
    """See module docstring. Construct once per workspace with its
    already-constructed collaborators (this module builds none of them
    itself, matching `ConnectivityMonitor`'s own "takes a `ProviderRegistry`,
    builds nothing else" shape)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_manager: MCAWorkspaceManager,
        principal: MCAPrincipal,
        provider_registry: ProviderRegistry,
        key_exchange: KeyExchangeCoordinator,
        connectivity_monitor: ConnectivityMonitor,
        probe_registry: Optional[ProbeRegistry] = None,
        delivery_adapter: Optional[DeliveryAdapter] = None,
        relay_client_factory: Optional[RelayClientFactory] = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        max_per_tick: int = MAX_ATTACHMENTS_PER_TICK,
        now_fn=time.time,
        lock: Optional[threading.Lock] = None,
        inbound_queue: Optional["queue.Queue[InboundEvent]"] = None,
        command_queue: Optional[CommandQueue] = None,
        command_registry: Optional[CommandRegistry] = None,
        dispatcher: Optional[CommandDispatcher] = None,
        snapshot_publisher: Optional[AttachmentsSnapshotPublisher] = None,
        wake_event: Optional[threading.Event] = None,
        ready_event: Optional[threading.Event] = None,
        pending_reservations: Optional[PendingReservations] = None,
        recipient_snapshot_publisher: Optional[RecipientSnapshotPublisher] = None,
    ):
        self._conn = conn
        self._workspace_manager = workspace_manager
        self._principal = principal
        self._provider_registry = provider_registry
        self._key_exchange = key_exchange
        self._connectivity = connectivity_monitor
        self._probe_registry = probe_registry if probe_registry is not None else ProbeRegistry()
        self._delivery_adapter = delivery_adapter
        self._relay_client_factory = relay_client_factory or self._default_relay_client
        self._tick_seconds = tick_seconds
        self._max_per_tick = max_per_tick
        self._now = now_fn
        self._pending_reservations = pending_reservations
        # Finding 1 (Correction 3): track committed creates that have not yet
        # been published in the snapshot as *records* (client_request_id ->
        # attachment_id + canonical_hash), not a set of ids. These are cleaned
        # up after _refresh_snapshot() confirms the idempotency entry for that
        # exact (attachment_id, canonical_hash) pair is visible in the
        # published snapshot - covering both a newly-inserted row and a
        # matching-hash duplicate recovered via the IntegrityError path.
        self._committed_reservations: Dict[str, _CommittedHandoff] = {}
        # Finding 3: ids of staged spool files whose removal failed on the
        # worker, for a bounded per-tick retry (see _drain_spool_cleanup).
        self._spool_cleanup_backlog: set[str] = set()
        # Finding 5, cadence gate: the `_now()` timestamp of the last orphan
        # sweep, or `None` before the first tick (which always runs the sweep).
        # See `_should_run_orphan_recovery()`.
        self._last_orphan_recovery_at: Optional[float] = None

        # PR #231 review, section 2: this service's own worker thread is
        # now the SOLE owner of `conn` at runtime - the radio listener no
        # longer touches it at all (it only enqueues an InboundEvent, see
        # enqueue_inbound()/_drain_inbound_events() below), so there is no
        # second thread left to race against for database access. `_lock`
        # is kept only to make `tick()` itself safely re-entrant (its
        # module docstring already documents "safe to call directly...
        # as well as from the worker thread" - e.g. a synchronous startup
        # pass, or a future direct call from a test), not because two
        # different owners still need to be serialized against each
        # other. A caller that constructs its own dedicated `conn` (every
        # test in this module does) can omit `lock` and get a private
        # one.
        self._lock = lock if lock is not None else threading.Lock()

        # PR #231 review, section 2: the bounded inbound-event queue -
        # the radio listener's only touchpoint with this service. Callers
        # that already have their own queue (mca_runtime.py, so events
        # queued before ensure_service() has even run are not lost - see
        # that module's own docstring) pass it in; a standalone/test
        # caller gets a private one.
        self._inbound_queue: "queue.Queue[InboundEvent]" = (
            inbound_queue if inbound_queue is not None else queue.Queue(maxsize=INBOUND_QUEUE_MAXSIZE)
        )

        # Step 1.6A.1 (correction #1/#2): the worker-owned command path and
        # snapshot publisher. Every one of these is *worker-owned* - the
        # request thread reaches them only through the facade (facade.py),
        # never directly, and never through `conn`. `mca_runtime.py` passes
        # its own single instances (so the facade it also constructs shares
        # the exact same queue/registry/publisher/wake_event); a standalone
        # caller that constructs its own dedicated `conn` (every test in
        # this module does) omits them and gets private defaults, the same
        # "omit `lock` and get a private one" shape as `_lock` above.
        self._command_queue = command_queue if command_queue is not None else CommandQueue()
        self._command_registry = command_registry if command_registry is not None else CommandRegistry()
        # Step 1.6A.3A: when no dispatcher is injected, build the real one
        # from this service's own lifecycle-command handlers (the worker-owned
        # collaborators live on this instance, so the handlers are its methods).
        # A caller that passes an explicit dispatcher (mca_runtime, or a test
        # injecting a custom table) keeps it unchanged.
        self._dispatcher = dispatcher if dispatcher is not None else self._build_dispatcher()
        self._snapshot_publisher = (
            snapshot_publisher if snapshot_publisher is not None else AttachmentsSnapshotPublisher()
        )
        self._wake_event = wake_event if wake_event is not None else threading.Event()
        # Finding 7 (Step 1.6A.3B review): the worker-refreshed recipient
        # snapshot publisher. `mca_runtime` passes the *same* instance it
        # handed the facade, so a request thread's synchronous recipient check
        # reads exactly the snapshot this worker refreshes. A standalone caller
        # that constructs its own dedicated `conn` (every test in this module
        # does) omits it; `None` means the per-tick recipient refresh is a
        # no-op (there is no request-facing facade to feed in that case).
        self._recipient_publisher = recipient_snapshot_publisher
        # Step 1.6A.1 (correction #1): the shared runtime-readiness signal.
        # `mca_runtime` passes the same Event it hands the facade, so the
        # facade's snapshot-backed reads/write are gated on exactly this
        # service's started-and-first-snapshot-published state. A standalone
        # caller that omits it gets a private one (and its own facade, if
        # any, would need to share it - see _publish_readiness()).
        self._ready_event = ready_event if ready_event is not None else threading.Event()
        self._started = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ------------------------------------------------------

    def start(self) -> None:
        """Idempotent: calling start() on an already-running service is a
        no-op, not a second thread. Marks the service as started (the first
        half of runtime readiness - the second is the first successful
        snapshot publish, applied by the worker's first tick via
        `_publish_readiness()`)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._started = True
        self._thread = threading.Thread(target=self._run, name="mca-attachments-worker", daemon=True)
        self._thread.start()

    def stop(self, *, timeout: Optional[float] = 5.0) -> bool:
        """Returns True if the worker thread actually stopped within
        `timeout`, False otherwise. PR #231 review: the old version
        unconditionally set `self._thread = None` after `join(timeout=...)`
        regardless of whether the thread had actually exited - a caller
        that then closed `conn` (mca_runtime.reset_state_for_tests()) could
        race a still-running worker mid-tick. Callers must check the
        return value before treating shutdown as complete; see
        reset_state_for_tests()'s own handling."""
        self._stop_event.set()
        self._wake_event.set()
        # Step 1.6A.1 (correction #1): readiness is cleared the moment the
        # service stops (or is asked to stop), so a request thread observing
        # a stopped service gets `FacadeNotReady`, never a stale "ready"
        # signal or a fabricated empty snapshot. Cleared before the join so
        # even a still-joining worker can no longer present itself as ready.
        self._started = False
        self._ready_event.clear()
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        stopped = not self._thread.is_alive()
        if stopped:
            self._thread = None
        else:
            logger.warning(
                "AttachmentsService worker did not stop within %ss (workspace_id=%s) - "
                "not clearing the thread handle or touching the connection",
                timeout, self._principal.workspace_id,
            )
        return stopped

    def wake(self) -> None:
        """The only method API handlers (once Step 1.6A exists) are meant
        to call after a domain-layer action. Never blocks, never touches
        the database or network - just flags the worker thread's wait()
        to return early."""
        self._wake_event.set()

    def evaluate_upload_readiness(
        self, provider_id: str, *, ciphertext_bytes: Optional[int] = None, requested_ttl_seconds: Optional[int] = None
    ):
        """PR #231 review (3rd pass), "contextual upload readiness":
        thin, read-only delegation to `ConnectivityMonitor.
        evaluate_upload_decision()` - the service-layer surface Step
        1.6A's future REST endpoints (`GET /api/mca/providers/
        <id>/upload-readiness` or similar - not built yet) should call
        rather than reaching into `self._connectivity` directly, matching
        this module's own "API handlers never touch the Relay/radio
        themselves" rule (module docstring) - a handler validates input,
        calls this, and returns the structured `UploadDecision` as JSON.
        Never blocks, never performs network I/O, and - PR #231 review
        (4th pass), "preserve the single-owner SQLite model" - never
        touches `self._conn`/SQLite either, from whatever thread calls
        it: `evaluate_upload_decision()` itself only reads
        `ConnectivityMonitor`'s own atomically-published, in-memory
        `_profile_snapshot` (built once at construction time and
        refreshed on every `refresh()` tick - never on a request thread;
        see that method's own docstring). This is genuinely safe to call
        from a future Flask REST request thread, not just documented as
        if it were - confirmed by a dedicated thread-identity test
        (`tests/test_connectivity_monitor.py`) that a simulated REST call
        performs zero SQLite operations."""
        return self._connectivity.evaluate_upload_decision(
            provider_id, ciphertext_bytes=ciphertext_bytes, requested_ttl_seconds=requested_ttl_seconds
        )

    def enqueue_inbound(self, event: InboundEvent) -> bool:
        """The radio listener's ONLY touchpoint with this service (PR #231
        review, section 2) - called from mca_runtime.handle_incoming_
        meshtastic_text(), on the radio listener thread, never on this
        service's own worker thread. Never blocks: `queue.Queue.put_nowait()`
        either succeeds immediately or raises `queue.Full`, which is
        caught here and turned into a logged, dropped event rather than
        ever blocking the caller. The log line deliberately omits `text`
        (the raw MCA1-TEXT payload) - only `source_address` (a Meshtastic
        node id, not a secret) is logged, per the review's own "logged
        without including message contents, keys, tokens" requirement.
        Returns True if the event was queued, False if it was dropped."""
        try:
            self._inbound_queue.put_nowait(event)
        except queue.Full:
            logger.warning(
                "AttachmentsService: inbound queue full (maxsize=%d) - dropping event from %s",
                INBOUND_QUEUE_MAXSIZE, event.source_address,
            )
            return False
        self.wake()
        return True

    def _run(self) -> None:
        logger.info("AttachmentsService worker started (workspace_id=%s)", self._principal.workspace_id)
        # PR #231 review, section 3: the first tick must run immediately,
        # not after waiting a full tick_seconds for the first wake()/
        # timeout - Event.wait(timeout=...) on a not-yet-set Event always
        # blocks for the full timeout, so without this explicit call the
        # very first tick would only happen after DEFAULT_TICK_SECONDS
        # (5s) had already elapsed.
        try:
            self.tick()
        except Exception:  # noqa: BLE001 - the worker thread must never die from one bad tick
            logger.exception("AttachmentsService initial tick failed")
        while not self._stop_event.is_set():
            self._wake_event.wait(timeout=self._tick_seconds)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - the worker thread must never die from one bad tick
                logger.exception("AttachmentsService tick failed")

    # ---- one tick ---------------------------------------------------------

    def tick(self) -> int:
        """One reconciliation pass: drain queued inbound events, then up
        to `max_per_tick` non-terminal attachments, one run_step() call
        each. Safe to call directly (a synchronous pass at startup right
        after resume_pending()/reconcile_pending(), or from a test) as
        well as from the worker thread - the lock makes repeated/
        concurrent calls to this method itself safely re-entrant."""
        with self._lock:
            return self._tick_locked()

    def _tick_locked(self) -> int:
        # PR #231 review, section 2: CBOR decoding, signature
        # verification, TOFU processing, attachment creation, and ACK
        # creation for an inbound radio message all happen right here -
        # on this worker thread, holding this connection - never on the
        # radio listener thread, which only ever built the InboundEvent
        # and enqueued it (enqueue_inbound() above).
        self._drain_inbound_events()

        # Finding 7 (Step 1.6A.3B review): republish the recipient-binding
        # snapshot after this tick's inbound events (a KEY_ANNOUNCE can create
        # or update a binding via KeyExchangeCoordinator) so a request thread's
        # synchronous recipient check reads a current view. Placed before the
        # command drain deliberately: a `attachment_create` drained this tick
        # does its own authoritative `get_binding()` re-check against the live
        # row, never against this snapshot.
        self._refresh_recipient_snapshot()

        # Step 1.6A.1 (correction #1): drain the bounded command queue
        # *before* the automatic-state row scan - the documented §3.2 tick
        # order (a queued `attachment_create`/`attachment_cancel`/... must
        # be applied before this same tick's row-scan sees the rows it
        # changed, so the scan reflects the command, never a stale state).
        self._drain_commands()

        # PR 1 (recoverable missing-key workflow): after draining user
        # commands (so a `contact_request_key`/`contact_confirm` already
        # drained this tick is reflected first), proactively request the key
        # for any received transfer still parked in WAITING_KEY with an
        # unknown signer. Rate-limited and idempotent - safe on every tick.
        self._auto_request_missing_keys()

        # The only network I/O this tick performs itself beyond the
        # inbound-event dispatch above - everything after this is either
        # a cheap DB scan or a run_step() call, which does its own I/O
        # only when a row is actually due.
        self._connectivity.refresh()

        processed = 0
        for row in self._due_rows():
            try:
                if row["direction"] == "sent":
                    self._step_sent(row)
                else:
                    self._step_received(row)
            except Exception:  # noqa: BLE001 - one bad attachment must not stop the whole tick
                logger.exception(
                    "AttachmentsService: run_step failed for attachment %s (direction=%s)",
                    row["id"],
                    row["direction"],
                )
            processed += 1

        # PR 2.5 (outbound EXPIRED): after the automatic row-scan, sweep
        # received attachments whose authoritative hard deadline has passed into
        # EXPIRED + a signed EXPIRED frame, before the reply dispatch below so a
        # just-enqueued EXPIRED is sent this same tick.
        self._reconcile_receiver_expiry()

        self._dispatch_outgoing_replies()

        # Step 1.6A.1 (correction #1): republish the attachment snapshot
        # *after* this tick's state transitions, so the request-facing read
        # surface (facade.py) reflects every row this tick advanced.
        self._refresh_snapshot()

        # Finding 1: clean up committed reservations now that the snapshot
        # has been published. The published snapshot's idempotency index
        # contains the committed entries, so we can safely remove the
        # in-memory pending reservations for those client_request_ids.
        self._cleanup_committed_reservations()

        # Finding 3: retry any backlogged spool-file removals, bounded per tick.
        self._drain_spool_cleanup()

        # ADR-0009 Decision 5: retire expired retained-revoke-capability rows,
        # bounded per tick (mca_sender_revoke_state is not a projected table, so
        # this needs no snapshot refresh).
        self._cleanup_expired_revoke_state()

        # Finding 5: bounded orphan-staging recovery - delete only old,
        # unreferenced staged files left behind by a crash, never an active
        # request's fresh file or a committed attachment's spool. Cadence-
        # gated: first tick, then at most once per ORPHAN_RECOVERY_CADENCE_SECONDS.
        if self._should_run_orphan_recovery():
            self._recover_orphaned_spool()

        # Step 1.6A.1 (correction #1): flip runtime readiness once this tick
        # has both a started service and a successfully-published snapshot.
        self._publish_readiness()

        return processed

    def _publish_readiness(self) -> None:
        """Step 1.6A.1 (correction #1): the second half of runtime readiness.
        Readiness is true only when the service is *started* (`start()` was
        called and the worker thread launched - the `_started` flag) *and*
        the first attachment snapshot has been published successfully (the
        publisher's `snapshot()` is no longer `None`). A failed first build
        leaves `snapshot()` as `None`, so readiness stays false and the next
        tick retries. Called from the end of every tick (and nowhere else),
        so the ready signal is always re-derived from the two facts, never
        remembered across a stop/start cycle."""
        if self._started and self._snapshot_publisher.snapshot() is not None:
            self._ready_event.set()

    def _refresh_recipient_snapshot(self) -> None:
        """Finding 7 (Step 1.6A.3B review): refresh the worker-published
        recipient-binding snapshot from the live binding table (via
        `KeyExchangeCoordinator.list_bindings()`, a worker-thread SQLite
        read). A no-op when no publisher was injected (a standalone service
        with its own dedicated `conn` and no request-facing facade to feed).
        Never raises - a transient read failure must not kill the tick; the
        request thread's synchronous check simply keeps the last-known-good
        snapshot, and the worker's authoritative `get_binding()` re-check
        still enforces the trust rule at commit time."""
        if self._recipient_publisher is not None:
            self._recipient_publisher.refresh()

    def _drain_inbound_events(self) -> None:
        """Up to MAX_INBOUND_EVENTS_PER_TICK events, oldest first - the
        rest simply wait for the next tick/wake(), the same bounded-work-
        per-tick discipline `_due_rows()`'s own `max_per_tick` already
        uses. One malformed/exception-raising event is caught and logged
        here, same as one bad attachment in the row-scan above - it must
        never stop the rest of this drain or kill the worker thread."""
        for _ in range(MAX_INBOUND_EVENTS_PER_TICK):
            try:
                event = self._inbound_queue.get_nowait()
            except queue.Empty:
                return
            try:
                self._process_one_inbound_event(event)
            except Exception:  # noqa: BLE001 - one bad inbound event must not stop the drain or the worker
                logger.exception(
                    "AttachmentsService: failed to process inbound event from %s", event.source_address
                )

    def _drain_commands(self) -> None:
        """Step 1.6A.1 (correction #1): drain up to `MAX_COMMANDS_PER_TICK`
        commands, oldest first, before the automatic-state row scan (§3.2's
        documented tick order). The rest simply wait for the next tick/wake(),
        the same bounded-work-per-tick discipline `_due_rows()`'s own
        `max_per_tick` already uses. One command whose handler raises is
        caught and marked terminal-failed inside `_execute_command()` - it
        can never stop the rest of this drain or kill the worker thread."""
        for _ in range(MAX_COMMANDS_PER_TICK):
            try:
                command = self._command_queue.get_nowait()
            except queue.Empty:
                return
            self._execute_command(command)

    def _execute_command(self, command: Command) -> None:
        """Step 1.6A.1 (correction #2/#4): execute one dequeued command by
        dispatching to its kind's handler, and record the terminal outcome
        in the registry. The worker is the sole executor (§3.2), and the
        dispatcher is the sole kind->work mapping - there is no arbitrary
        callable command here. Every failure mode is caught and turned into
        a terminal transition:

        - an enumerated-but-unwired kind: the dispatcher itself returns
          `CommandOutcome.failed(unsupported_command_kind)` (dispatch.py),
          so this method just records that stable terminal FAILED;
        - a handler that *raises* (a bug): caught here and recorded as a
          terminal FAILED with `COMMAND_EXECUTION_FAILED`;
        - a handler that returns the *wrong type* (not a `CommandOutcome`):
          a bug, recorded as `COMMAND_EXECUTION_FAILED` - never an
          `AttributeError` escaping the drain loop;
        - a handler that constructs an *invalid* outcome (empty/non-
          snake_case error_code, or a failed shape carrying resource_id/
          result): `CommandOutcome.__post_init__` raises, which the dispatch
          try/except below converts to `COMMAND_EXECUTION_FAILED`;
        - an *invalid result payload* (non-serializable / oversized): the
          registry itself (`mark_succeeded`) records a terminal `failed`
          with `result_payload_not_serializable`/`result_payload_too_large`
          rather than raising - the specific payload errors are preserved;
        - a *registry transition error* (an impossible `running`->terminal
          transition, or a `mark_running` that fails): `_record_succeeded`/
          `_record_failed`/this method fall back to the registry's
          internal-recovery fail-safe (`CommandRegistry.record_internal_
          failure`), which terminalizes the entry to `command_execution_
          failed` instead of leaving it stuck in `running` (see that method's
          docstring). Only if the fail-safe itself raises - registry
          corruption, an unrecoverable internal exception - is the command
          left without a terminal result, and that is logged, never
          propagated out of the drain loop.

        Either way one broken command never stops the rest of the drain and
        the tick continues."""
        try:
            self._command_registry.mark_running(command.command_id)
        except Exception as exc:  # noqa: BLE001 - an impossible transition must not kill the tick
            # Sanitized: no exception text/traceback (the exception may be
            # influenced by command input) - log only safe identifiers + class.
            logger.error(
                "AttachmentsService: could not mark command %s (%s) running: %s",
                command.command_id, command.kind, type(exc).__name__,
            )
            self._recover_internal_failure(command)
            return
        try:
            outcome = self._dispatcher.dispatch(command)
        except Exception as exc:  # noqa: BLE001 - a raising handler (or an invalid outcome) must not stop the drain
            # Sanitized: a handler exception's text may embed file names,
            # Relay tokens, keys, comments or other untrusted command input -
            # log only the safe identifier, kind and exception class.
            logger.error(
                "AttachmentsService: command %s (%s) handler raised %s",
                command.command_id, command.kind, type(exc).__name__,
            )
            # PR 2.5 correction (defense-in-depth): roll back any transaction
            # the raising handler left open, so a partially-written state
            # transition (or half-inserted outbox row) can never be committed
            # by a later, unrelated operation on this connection. The handler
            # is expected to roll back itself (see `_atomic`); this is the
            # belt-and-suspenders guarantee that even a handler that raises
            # before reaching its own rollback cannot leak a partial write.
            self._rollback_silently()
            self._record_failed(command, COMMAND_EXECUTION_FAILED)
            return
        if not isinstance(outcome, CommandOutcome):
            logger.error(
                "AttachmentsService: command %s handler returned %s, not a CommandOutcome",
                command.command_id, type(outcome).__name__,
            )
            self._record_failed(command, COMMAND_EXECUTION_FAILED)
            return
        if outcome.error_code is None:
            self._record_succeeded(command, outcome)
        else:
            self._record_failed(command, outcome.error_code)

    def _record_succeeded(self, command: Command, outcome: CommandOutcome) -> None:
        """Transition a command to terminal `succeeded`. A registry transition
        error here (an impossible state) is a worker bug, not a command error,
        and must not escape the drain loop. On such a failure the command is
        recovered through the registry's internal-recovery fail-safe
        (`CommandRegistry.record_internal_failure`) so it still reaches a
        terminal result instead of staying stuck in `running`; the fail-safe
        is a no-op against an already-terminal entry. `mark_succeeded` itself
        also converts an invalid result payload into a terminal `failed`
        (`result_payload_not_serializable`/`result_payload_too_large`) rather
        than raising."""
        try:
            self._command_registry.mark_succeeded(
                command.command_id, resource_id=outcome.resource_id, result=outcome.result
            )
        except Exception as exc:  # noqa: BLE001 - a transition error must not kill the tick
            logger.error(
                "AttachmentsService: could not mark command %s (%s) succeeded: %s",
                command.command_id, command.kind, type(exc).__name__,
            )
            self._recover_internal_failure(command)

    def _record_failed(self, command: Command, error_code: str) -> None:
        """Transition a command to terminal `failed`. A registry transition
        error here is a worker bug, not a command error, and must not escape
        the drain loop. On such a failure the command is recovered through the
        registry's internal-recovery fail-safe (`CommandRegistry.record_
        internal_failure`) so it still reaches a terminal result instead of
        staying stuck in `running`; the fail-safe is a no-op against an
        already-terminal entry."""
        try:
            self._command_registry.mark_failed(command.command_id, error_code=error_code)
        except Exception as exc:  # noqa: BLE001 - a transition error must not kill the tick
            logger.error(
                "AttachmentsService: could not mark command %s (%s) failed: %s",
                command.command_id, command.kind, type(exc).__name__,
            )
            self._recover_internal_failure(command)

    def _recover_internal_failure(self, command: Command) -> None:
        """Invoke the registry's internal-recovery fail-safe (final correction
        pass). It terminalizes a `queued`/`running` entry (or materializes a
        missing one) to `command_execution_failed` under the registry's own
        lock, and never overwrites an already-terminal result. It logs only
        safe identifiers (command_id and the enumerated kind) and the
        exception class, never the exception text, traceback, or payload.

        A failure *here* means the registry itself is corrupt - there is
        nothing left to record against - so the worker logs it and survives
        rather than raising. This is the one honest limit: a completely
        corrupted registry cannot be made to produce a terminal polling result,
        and that is documented rather than papered over."""
        try:
            self._command_registry.record_internal_failure(command)
        except Exception as exc:  # noqa: BLE001 - registry corruption; survive, do not raise
            logger.error(
                "AttachmentsService: could not record internal failure for command %s (%s) "
                "(registry corruption): %s",
                command.command_id, command.kind, type(exc).__name__,
            )

    # ---- lifecycle-command handlers (Step 1.6A.3A) -------------------------
    #
    # The three wired command kinds for the first mutation sub-stage. Each is a
    # plain `CommandHandler` (a `Callable[[Command], CommandOutcome]`) bound to
    # this service, so the worker-owned collaborators (conn, workspace_manager,
    # principal, provider_registry, key_exchange, connectivity_monitor,
    # delivery_adapter, relay_client_factory, now_fn) stay on the worker side
    # and the dispatcher remains a fixed kind->callable table (§3.2). None of
    # these is ever invoked on a request thread. They re-validate the persisted
    # row rather than trusting the request thread's snapshot, so a row that
    # advanced between the synchronous precondition check and this command's
    # execution is caught here, not silently acted on.

    def _build_dispatcher(self) -> CommandDispatcher:
        """The real kind->handler table the worker runs (replaces the empty
        placeholder this service would otherwise default to). The six
        attachment/contact command handlers wired so far - the idempotent
        create (`attachment_create`, Step 1.6A.3B), the three Step 1.6A.3A
        lifecycle commands (retry/download/reject), and the two Step 1.6A.3C
        mutations (cancel and contact key request) - the three Step 1.7
        contact-trust commands (`contact_confirm`/`contact_accept_key_change`/
        `contact_reject_key_change`, Files workspace) - plus the eight Step
        1.6A.4 provider commands (`provider_probe`/`provider_register`/
        `provider_update`/`provider_set_default`/`provider_remove`/
        `provider_set_upload_token`/`provider_clear_upload_token`/
        `provider_check`). Every other enumerated kind stays
        `unsupported_command_kind` until its own future sub-stage wires it.
        Built once at construction (the dispatcher snapshots its mapping),
        never mutated afterwards."""
        return CommandDispatcher({
            "attachment_create": self._command_create,
            "attachment_retry": self._command_retry,
            "attachment_download": self._command_download,
            "attachment_reject": self._command_reject,
            "attachment_cancel": self._command_cancel,
            "attachment_save": self._command_save,
            "attachment_revoke": self._command_revoke,
            "attachment_delete_local_content": self._command_delete_local_content,
            "contact_request_key": self._command_request_key,
            "contact_confirm": self._command_confirm,
            "contact_accept_key_change": self._command_accept_key_change,
            "contact_reject_key_change": self._command_reject_key_change,
            "provider_probe": self._command_provider_probe,
            "provider_register": self._command_provider_register,
            "provider_update": self._command_provider_update,
            "provider_set_default": self._command_provider_set_default,
            "provider_remove": self._command_provider_remove,
            "provider_set_upload_token": self._command_provider_set_upload_token,
            "provider_clear_upload_token": self._command_provider_clear_upload_token,
            "provider_check": self._command_provider_check,
        })

    def _command_create(self, command: Command) -> CommandOutcome:
        """`attachment_create` (Step 1.6A.3B, §7.2): materialize the outgoing
        draft the request thread already staged, minted ids for, and reserved
        (§3.5/§3.6). The request thread wrote the plaintext to
        `spool/outgoing/<attachment_id>` and handed over `attachment_id`/
        `command_id`/`canonical_hash` plus the validated metadata; this is the
        worker-side half that (a) re-validates the complete payload (Finding 3,
        below), (b) resolves the recipient's *trusted* binding, and (c) calls
        `sender.create_draft()` with the already-minted ids and the precomputed
        idempotency columns.

        Recipient trust is resolved here, on the worker, because
        `key_exchange.get_binding()` reads SQLite (worker-owned, §3.1) - it
        cannot be checked synchronously on the request thread. §7.2's
        "recipient binding must be trusted" is therefore enforced at this
        stage, failing with `recipient_not_found` (no binding) /
        `recipient_not_trusted` (binding present but not `MCA_READY`),
        observed via the command result. Every other §7.2 check was already
        validated pure on the request thread (id shape, comment bound,
        provider resolution, TTL range, file size, MIME sniff) - and is
        re-validated here defensively (Finding 3), since the worker is the
        sole executor and must not trust the queue.

        `adapter_id`/`connector_profile_id` are the MVP single-transport
        `"meshtastic"` literal (matching `mca_runtime.ADAPTER_ID` and the
        request thread's own canonical-hash inputs), and `route_type`/
        `route_id` are the DIRECT route to `source_address` - all four are
        derived deterministically here rather than trusted from the payload,
        so they cannot disagree with the hash the request thread computed.

        The pending reservation is kept until AFTER the snapshot publisher
        has published the committed idempotency entry (Finding 1: publish-
        before-release). On every failure before a successful commit, the
        transaction is rolled back, the reservation removed, and the staged
        file removed via the bounded cleanup path (Finding 3) - no partial
        rows, no stale reservation, no orphaned plaintext, and never a success
        result while the newly-unused plaintext is known to still exist."""
        payload = self._validated_create_payload(command)
        if payload is None:
            self._create_failure_cleanup(command)
            return CommandOutcome.failed("invalid_payload")

        attachment_id = payload["attachment_id"]
        client_request_id = payload["client_request_id"]
        source_address = payload["source_address"]

        # Finding 6: the request thread validated provider policy from its own
        # immutable snapshot; the profile may have been removed/disabled, or its
        # upload policy/token changed, in the window since. Re-resolve from the
        # worker-owned registry and recheck the *same* local configuration
        # immediately before committing the draft - a worker must not trust the
        # queue, and must not commit a draft to a provider that is no longer
        # uploadable. On any drift, fail safely and clean the stage (the same
        # total cleanup as every other pre-commit failure, Finding 3).
        provider = self._provider_registry.resolve(payload["provider_id"])
        if provider is None:
            self._create_failure_cleanup(command)
            return CommandOutcome.failed("provider_not_found")
        if not provider.enabled:
            self._create_failure_cleanup(command)
            return CommandOutcome.failed("provider_disabled")
        if not provider.upload_allowed:
            self._create_failure_cleanup(command)
            return CommandOutcome.failed("upload_not_allowed")
        if not provider.upload_token_configured:
            self._create_failure_cleanup(command)
            return CommandOutcome.failed("upload_token_missing")
        # Provider size policy, from the worker's fresh profile: reject when the
        # deterministic ciphertext upper bound for the staged plaintext already
        # exceeds `max_ciphertext_bytes`. "When possible" - if the staged file
        # is unexpectedly gone the check is skipped here and the authoritative
        # post-encryption limit (`create_upload`'s `total_size`) still runs
        # before upload; a missing source also fails VALIDATING independently.
        spool_path = self._spool_path_for(attachment_id)
        plain_size = None
        if spool_path is not None:
            try:
                plain_size = spool_path.stat().st_size
            except OSError:
                plain_size = None
        if plain_size is not None and crypto.ciphertext_size(plain_size) > provider.max_ciphertext_bytes:
            self._create_failure_cleanup(command)
            return CommandOutcome.failed("ciphertext_too_large")

        try:
            binding = self._key_exchange.get_binding(source_address)
            if binding is None:
                self._create_failure_cleanup(command)
                return CommandOutcome.failed("recipient_not_found")
            if binding.status != AddressStatus.MCA_READY:
                self._create_failure_cleanup(command)
                return CommandOutcome.failed("recipient_not_trusted")

            target = sender.RecipientTarget(
                public_identity=binding.public_identity,
                key_id=binding.sender_key_id,
            )
            sender.create_draft(
                self._conn,
                self._workspace_manager,
                self._principal,
                workspace_id=self._principal.workspace_id,
                source_path=str(self._spool_path_for(attachment_id)),
                file_name=payload["source_name"],
                mime_type=payload["mime_type"],
                recipients=[target],
                adapter_id="meshtastic",
                connector_profile_id="meshtastic",
                route_type=RouteType.DIRECT.value,
                route_id=source_address,
                provider_id=decode_provider_id(payload["provider_id"]),
                comment=payload["comment"],
                hard_ttl_seconds=payload["hard_ttl_seconds"],
                download_grace_seconds=payload["download_grace_seconds"],
                attachment_id=attachment_id,
                client_request_id=client_request_id,
                canonical_hash=payload["canonical_hash"],
                now=self._now(),
            )
        except sqlite3.IntegrityError as exc:
            # The INSERT failed - could be the idempotency index or another
            # constraint. Roll back the failed transaction first, then determine
            # the cause by loading the existing row (Finding 2), never leaking
            # the SQLite exception text or class.
            self._rollback_silently()
            removed = self._remove_unreferenced_spool(attachment_id)
            self._conn.row_factory = sqlite3.Row
            row = self._conn.execute(
                "SELECT id, state, canonical_hash FROM attachments "
                "WHERE workspace_id = ? AND client_request_id = ?",
                (self._principal.workspace_id, client_request_id),
            ).fetchone()
            if row is None:
                # Not the idempotency constraint - some other unique violation
                # (e.g. a primary key collision, which should be astronomically
                # unlikely). Re-raise as a generic internal error; never leak
                # the SQLite exception text or class.
                self._remove_reservation(client_request_id)
                logger.error(
                    "AttachmentsService: unexpected IntegrityError on create_draft "
                    "(not the idempotency index): %s", type(exc).__name__
                )
                raise RuntimeError("attachment_create: database constraint violation")
            # Found the existing row - this IS the idempotency constraint.
            # Only return idempotent success if the canonical_hash matches
            # exactly; otherwise it's a genuine content conflict.
            existing_hash = row["canonical_hash"]
            if existing_hash is not None and existing_hash == payload["canonical_hash"]:
                if not removed:
                    # Finding 3: do not report success while the newly-unused
                    # duplicate plaintext is known to still exist.
                    self._remove_reservation(client_request_id)
                    return CommandOutcome.failed("spool_cleanup_failed")
                # Finding 1 (Correction 3): a matching-hash duplicate. Promote
                # the reservation to the *original* row's id and record a
                # committed handoff, so a concurrent replay returns the original
                # attachment (not the colliding request's id) until the
                # committed snapshot publishes that exact (id, hash) pair.
                self._promote_duplicate_reservation(
                    command, client_request_id, row["id"], existing_hash
                )
                return CommandOutcome.succeeded(
                    resource_id=row["id"],
                    result={"attachment_id": row["id"], "state": row["state"]},
                )
            # Hash mismatch - terminal idempotency_conflict. The staged file
            # is discarded; no row is created for this request, so the
            # reservation is dropped (the request is terminal, not a replay).
            self._remove_reservation(client_request_id)
            return CommandOutcome.failed("idempotency_conflict")
        except Exception:
            # Finding 3: any other exception before a successful commit - roll
            # back, remove the reservation, and remove the staged file.
            self._create_failure_cleanup(command)
            raise

        # Finding 1 (Correction 3): successful commit - record the handoff
        # (client_request_id + attachment_id + canonical_hash) for cleanup
        # after the snapshot publishes that exact triple.
        self._record_committed_handoff(client_request_id, attachment_id, payload["canonical_hash"])
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "state": sender.DRAFT},
        )

    # ---- create failure/cleanup helpers (Finding 3) ------------------------

    def _validated_create_payload(self, command: Command) -> Optional[dict]:
        """Finding 3: re-validate the complete `attachment_create` payload on
        the worker before it is trusted for any filesystem path or database
        write. The request thread already validated every field; this is the
        worker-side backstop so a malformed command (never expected, but the
        worker must not trust the queue) fails cleanly with `invalid_payload`
        rather than touching the spool dir or writing a partial row.

        Returns a normalized dict of validated values, or None if any field is
        invalid. The command kind is checked first, so an
        `attachment_create`-routed command that is not actually an
        `attachment_create` (a dispatcher bug) is also rejected here."""
        if command.kind != "attachment_create":
            return None
        payload = command.payload

        attachment_id = payload.get("attachment_id")
        if not isinstance(attachment_id, str) or _HEX32_RE.match(attachment_id) is None:
            return None

        client_request_id = payload.get("client_request_id")
        try:
            validate_client_request_id(client_request_id)
        except ValueError:
            return None

        canonical_hash = payload.get("canonical_hash")
        try:
            validate_canonical_hash(canonical_hash)
        except ValueError:
            return None

        source_address = _bounded_text(
            payload.get("source_address"), max_len=64, allow_empty=False
        )
        if source_address is None:
            return None
        source_name = _bounded_text(
            payload.get("source_name"), max_len=255, allow_empty=False
        )
        if source_name is None:
            return None
        mime_type = _bounded_text(
            payload.get("mime_type"), max_len=128, allow_empty=False
        )
        if mime_type is None:
            return None

        provider_id_text = payload.get("provider_id")
        if not isinstance(provider_id_text, str):
            return None
        try:
            # Decode to 8 raw bytes and require canonical round-trip equality,
            # mirroring api_attachments._validate_provider_id - a padded,
            # wrong-length, or wrong-alphabet spelling is rejected, not coerced.
            if encode_provider_id(decode_provider_id(provider_id_text)) != provider_id_text:
                return None
        except ProviderRegistryError:
            return None

        comment = payload.get("comment")
        if comment is not None:
            if not isinstance(comment, str) or "\x00" in comment:
                return None
            try:
                comment_bytes = len(comment.encode("utf-8", errors="strict"))
            except UnicodeEncodeError:
                return None
            if comment_bytes > sender.MAX_COMMENT_BYTES:
                return None

        hard_ttl_seconds = payload.get("hard_ttl_seconds")
        if (
            not isinstance(hard_ttl_seconds, int)
            or isinstance(hard_ttl_seconds, bool)
            or hard_ttl_seconds <= 0
        ):
            return None
        download_grace_seconds = payload.get("download_grace_seconds")
        if (
            not isinstance(download_grace_seconds, int)
            or isinstance(download_grace_seconds, bool)
            or download_grace_seconds <= 0
        ):
            return None

        return {
            "attachment_id": attachment_id,
            "client_request_id": client_request_id,
            "canonical_hash": canonical_hash,
            "source_address": source_address,
            "source_name": source_name,
            "mime_type": mime_type,
            "provider_id": provider_id_text,
            "comment": comment,
            "hard_ttl_seconds": hard_ttl_seconds,
            "download_grace_seconds": download_grace_seconds,
        }

    def _spool_path_for(self, attachment_id) -> Optional[Path]:
        """The spool path for a staged `attachment_id`, derived *only* from a
        strictly-validated 32-hex id - never from any browser-supplied
        filename or other untrusted component (Finding 3/4). Returns None for a
        non-32-hex id, so a malformed id can never be used to build a path that
        escapes `spool/outgoing/`."""
        if not isinstance(attachment_id, str) or _HEX32_RE.match(attachment_id) is None:
            return None
        return self._workspace_manager.paths(self._principal.principal_id).spool_outgoing / attachment_id

    def _rollback_silently(self) -> None:
        """Roll back the worker's SQLite connection, swallowing any error (the
        connection may already be in an unusable state). Called on every create
        failure before a successful commit so no partial row from a failed
        `create_draft` leaks into the next statement on this connection."""
        try:
            self._conn.rollback()
        except Exception:
            pass

    @contextlib.contextmanager
    def _atomic(self, name: str = "op"):
        """PR 2.5 correction (exception-atomic transitions): run a block as one
        atomic unit on the worker's single SQLite connection. Opens a SAVEPOINT,
        then on success releases it and commits the outer transaction, and on
        any exception rolls back *to the savepoint* and releases it - so a
        partial write from the block (a state UPDATE, an `attachment_events`
        row, a sender transient/revoke-state DELETE, a half-inserted outbox
        row) can never leak into a later commit on this connection.

        Distinct from `_rollback_silently()`, which undoes *everything* since
        the last commit: a SAVEPOINT rollback is scoped to exactly this block,
        so a caller looping over many rows (the expiry sweep) can isolate one
        failing row and keep processing later ones without committing the
        failed row's partial writes. The worker is single-threaded and every
        other operation commits or rolls back before yielding, so `_atomic`'s
        savepoint is normally the outermost transaction - but it is correct
        even when a caller's own DML opened an outer transaction first (the
        savepoint is then nested and only the inner block is undone)."""
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO {name}")
            self._conn.execute(f"RELEASE {name}")
            raise
        self._conn.execute(f"RELEASE {name}")
        self._conn.commit()

    def _remove_reservation(self, client_request_id) -> None:
        """Drop the pending reservation for a create that will not commit a row
        (or whose row already exists). Guarded: a malformed/non-string key is
        skipped rather than raising, and the reservation map is optional."""
        if isinstance(client_request_id, str) and self._pending_reservations is not None:
            self._pending_reservations.remove(client_request_id)

    def _remove_unreferenced_spool(self, attachment_id) -> bool:
        """Attempt to remove the staged spool file for a create that did not
        commit a row referencing it (invalid payload, recipient failure,
        duplicate, or conflict). Returns True if the file no longer exists
        (removed or already gone); False if the unlink failed and the file is
        still present. Never raises. On failure the id is retained in the
        bounded cleanup backlog for a per-tick retry (Finding 3)."""
        spool_path = self._spool_path_for(attachment_id)
        if spool_path is None:
            # No valid path to clean - nothing was staged under a valid id.
            return True
        try:
            spool_path.unlink(missing_ok=True)
            return True
        except OSError:
            logger.warning(
                "AttachmentsService: could not remove a staged-but-unused spool file"
            )
            self._request_spool_cleanup(attachment_id)
            return False

    def _request_spool_cleanup(self, attachment_id) -> None:
        """Retain a staged id whose unlink failed, for a bounded per-tick retry
        (Finding 3). Bounded so a burst of un-unlinkable files cannot grow an
        unbounded in-memory set; a full backlog is logged and dropped (the
        orphan is then reclaimed by Finding 5's bounded orphan-staging
        recovery)."""
        if len(self._spool_cleanup_backlog) < SPOOL_CLEANUP_BACKLOG_MAXSIZE:
            self._spool_cleanup_backlog.add(attachment_id)
        else:
            logger.warning(
                "AttachmentsService: spool cleanup backlog full; "
                "staged file left for orphan-staging recovery"
            )

    def _drain_spool_cleanup(self) -> None:
        """Retry up to `MAX_SPOOL_CLEANUP_PER_TICK` backlogged spool removals,
        bounded so one tick never stalls retrying the same un-unlinkable file
        indefinitely (Finding 3). A still-failing removal is put back (up to the
        backlog cap) for the next tick; a now-absent file simply drops off."""
        for _ in range(MAX_SPOOL_CLEANUP_PER_TICK):
            try:
                attachment_id = self._spool_cleanup_backlog.pop()
            except KeyError:
                return
            spool_path = self._spool_path_for(attachment_id)
            if spool_path is None:
                continue
            try:
                spool_path.unlink(missing_ok=True)
            except OSError:
                if len(self._spool_cleanup_backlog) < SPOOL_CLEANUP_BACKLOG_MAXSIZE:
                    self._spool_cleanup_backlog.add(attachment_id)

    def _cleanup_expired_revoke_state(self) -> None:
        """ADR-0009 Decision 5: delete retained revoke-capability rows whose
        `delete_after` bound has passed - the Relay object they protected is
        guaranteed gone (hard expiry + download grace), so the token has no
        remaining purpose. Bounded to `MAX_REVOKE_STATE_CLEANUP_PER_TICK` rows
        per tick, so a large backlog drains over successive ticks rather than
        blocking one. `delete_after` is a decimal-string epoch, so the
        comparison casts to INTEGER rather than relying on lexicographic order."""
        self._conn.execute(
            "DELETE FROM mca_sender_revoke_state WHERE attachment_id IN ("
            "  SELECT attachment_id FROM mca_sender_revoke_state "
            "  WHERE CAST(delete_after AS INTEGER) <= ? LIMIT ?"
            ")",
            (int(self._now()), MAX_REVOKE_STATE_CLEANUP_PER_TICK),
        )
        self._conn.commit()

    def _create_failure_cleanup(self, command: Command) -> None:
        """The total failure cleanup for a create that will not commit a row
        (Finding 3): roll back any in-flight transaction, drop the pending
        reservation, and request removal of the staged file. Never raises and
        never trusts payload values as filesystem components - the spool path is
        derived only from a validated 32-hex id."""
        self._rollback_silently()
        self._remove_reservation(command.payload.get("client_request_id"))
        self._remove_unreferenced_spool(command.payload.get("attachment_id"))

    def _record_committed_handoff(self, client_request_id: str, attachment_id: str, canonical_hash: str) -> None:
        """Finding 1 (Correction 3): record a committed create as a
        `(client_request_id, attachment_id, canonical_hash)` handoff, so its
        pending reservation is released only after the published snapshot's
        idempotency index shows that exact triple. Covers both a
        newly-inserted row and a matching-hash duplicate recovered via the
        IntegrityError path."""
        self._committed_reservations[client_request_id] = _CommittedHandoff(
            client_request_id=client_request_id,
            attachment_id=attachment_id,
            canonical_hash=canonical_hash,
        )

    def _promote_duplicate_reservation(
        self, command: Command, client_request_id: str, original_id: str, canonical_hash: str
    ) -> None:
        """Finding 1 (Correction 3): a matching-hash duplicate was found via the
        IntegrityError path. Correct the pending reservation in place so its
        `attachment_id` becomes the *original* row's id (not the colliding
        request's), then record a committed handoff - so a concurrent replay of
        the same `client_request_id` returns the original attachment and matching
        hash, with a valid command id, until the committed snapshot publishes the
        same `(attachment_id, canonical_hash)` pair and releases the reservation."""
        if self._pending_reservations is not None:
            self._pending_reservations.replace(
                client_request_id,
                PendingReservation(
                    canonical_hash=canonical_hash,
                    attachment_id=original_id,
                    command_id=command.command_id,
                ),
            )
        self._record_committed_handoff(client_request_id, original_id, canonical_hash)

    # ---- orphan-staging recovery (Finding 5) --------------------------------

    def _referenced_attachment_ids(self) -> set:
        """The complete set of attachment ids this worker must *not* treat as
        orphaned spool files: every persisted `attachments` row in this
        workspace (survives restart), every pending §3.6 reservation
        (in-memory), and every queued command's `attachment_id` (in-memory).
        A staged file whose name is in this set is referenced and preserved,
        no matter its age. Pure reads only - no filesystem writes, no
        network."""
        referenced = set()
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            "SELECT id FROM attachments WHERE workspace_id = ?",
            (self._principal.workspace_id,),
        ).fetchall()
        referenced.update(row["id"] for row in rows)
        if self._pending_reservations is not None:
            referenced.update(
                r.attachment_id for r in self._pending_reservations.snapshot_ids().values()
            )
        for command in self._command_queue.iter_commands():
            attachment_id = command.payload.get("attachment_id")
            if isinstance(attachment_id, str):
                referenced.add(attachment_id)
        return referenced

    def _should_run_orphan_recovery(self) -> bool:
        """Finding 5, cadence gate: decide whether *this* tick should run the
        orphan sweep. The sweep is O(N) over the spool directory, so it must
        not run on every wake-driven tick - it runs on the first tick after
        startup (so a fresh process reclaims the previous process's crash
        orphans), then at most once per `ORPHAN_RECOVERY_CADENCE_SECONDS`.
        Returns `True` (and records the attempt via `self._now()`) when the
        sweep should run, `False` otherwise. Pure bookkeeping - never raises,
        never touches the filesystem."""
        now = self._now()
        if (
            self._last_orphan_recovery_at is not None
            and now - self._last_orphan_recovery_at < ORPHAN_RECOVERY_CADENCE_SECONDS
        ):
            return False
        self._last_orphan_recovery_at = now
        return True

    def _recover_orphaned_spool(self) -> None:
        """Finding 5: bounded orphan-staging recovery. Scans *only* this
        workspace's `spool/outgoing/` directory for staged files left behind
        by a process crash after staging but before enqueue/commit, and
        deletes only the ones that are (a) old enough to be unambiguously
        abandoned (`ORPHAN_SPOOL_MIN_AGE_SECONDS`) and (b) unreferenced by any
        persisted row, pending reservation, or queued command - so an active
        request's fresh file and a committed attachment's file are both
        preserved, even across restart.

        Bounded: at most `MAX_ORPHAN_SCAN_PER_TICK` entries are examined and
        at most `MAX_ORPHAN_DELETE_PER_TICK` files deleted per tick. Never
        recurses or follows symlinks (so it cannot be led to an arbitrary
        path), and only ever deletes a name matching the two known
        conventions (`_classify_spool_name`). All logging is sanitized - no
        path, no identifier. Never raises: the whole pass is a best-effort
        disk-hygiene sweep, and one bad entry or failed unlink is logged and
        skipped, not allowed to stop the tick."""
        spool_dir = self._workspace_manager.paths(self._principal.principal_id).spool_outgoing
        referenced = self._referenced_attachment_ids()
        now = self._now()
        examined = 0
        deleted = 0
        try:
            with os.scandir(spool_dir) as entries:
                for entry in entries:
                    if examined >= MAX_ORPHAN_SCAN_PER_TICK or deleted >= MAX_ORPHAN_DELETE_PER_TICK:
                        break
                    examined += 1
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    kind = _classify_spool_name(entry.name)
                    if kind is None:
                        # Not a name this endpoint ever writes - leave it alone.
                        continue
                    try:
                        mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if now - mtime < ORPHAN_SPOOL_MIN_AGE_SECONDS:
                        continue  # active request - too fresh to be an orphan
                    if kind == "committed" and entry.name in referenced:
                        continue  # referenced by a row/reservation/command
                    try:
                        os.unlink(entry.path)
                    except OSError:
                        logger.warning(
                            "AttachmentsService: could not remove an orphaned spool file"
                        )
                        continue
                    deleted += 1
        except OSError:
            # Spool directory missing or unreadable - nothing to recover.
            return

    def _attachment_row(self, attachment_id: str) -> Optional[sqlite3.Row]:
        """The single persisted row a lifecycle command re-validates against,
        re-read from the worker-owned `conn` (its sole owner, §3.1). Returns
        `None` for an unknown id or a row in another workspace."""
        self._conn.row_factory = sqlite3.Row
        return self._conn.execute(
            "SELECT id, direction, provider_id, state FROM attachments "
            "WHERE id = ? AND workspace_id = ?",
            (attachment_id, self._principal.workspace_id),
        ).fetchone()

    def _command_retry(self, command: Command) -> CommandOutcome:
        """`attachment_retry`: re-drive the same per-row step path the tick
        runs, for one attachment the client wants advanced immediately (§7.3).
        Valid only in the row's own direction's `AUTOMATIC_STATES`; terminal
        `FAILED_*`/`REJECTED`/`EXPIRED`/`CANCELLED`/`REVOKED` are never
        retryable here (terminal-failure recovery is a future state-machine
        change, not this endpoint). Does not mint/replace `transfer_id` and
        does not bypass per-row provider selection - it calls the same
        `_step_sent`/`_step_received` the tick does, which absorb an
        unavailable radio/Relay into a retryable persisted state rather than
        raising."""
        attachment_id = command.payload.get("attachment_id")
        row = self._attachment_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        direction = row["direction"]
        state = row["state"]
        if direction == "sent":
            if state not in sender.AUTOMATIC_STATES:
                return CommandOutcome.failed("invalid_state_transition")
            self._step_sent(row)
        elif direction == "received":
            if state not in receiver.AUTOMATIC_STATES:
                return CommandOutcome.failed("invalid_state_transition")
            self._step_received(row)
        else:
            return CommandOutcome.failed("invalid_state_transition")
        new_state = (
            sender.get_state(self._conn, attachment_id)
            if direction == "sent"
            else receiver.get_state(self._conn, attachment_id)
        )
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "state": new_state},
        )

    def _command_download(self, command: Command) -> CommandOutcome:
        """`attachment_download`: the explicit user action that moves a
        received `WAITING_CONSENT` row to `DOWNLOADING` (§7.3). Consent is
        never weakened: both this worker-side re-check and
        `receiver.begin_download()`'s own guard require `WAITING_CONSENT`, so
        an attachment that already left consent (or was never received) cannot
        be force-downloaded here. Runs on the worker, not a request thread."""
        attachment_id = command.payload.get("attachment_id")
        row = self._attachment_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        if row["direction"] != "received" or row["state"] != receiver.WAITING_CONSENT:
            return CommandOutcome.failed("invalid_state_transition")
        try:
            receiver.begin_download(self._conn, attachment_id, now=self._now())
        except receiver.ReceiverError:
            # The row moved off WAITING_CONSENT between our read and the call.
            return CommandOutcome.failed("invalid_state_transition")
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "state": receiver.DOWNLOADING},
        )

    def _command_reject(self, command: Command) -> CommandOutcome:
        """`attachment_reject`: the explicit user action that moves a received
        `WAITING_CONSENT` row to `REJECTED` (§7.3). PR 2.5 closes the outbound
        half of the round trip: the local `WAITING_CONSENT -> REJECTED`
        transition and the enqueue of the signed `MessageType.REJECTED` frame
        to the pinned sender route happen in the **same** DB transaction (one
        commit, never two sequential ones). The invariant is both directions -
        no local REJECTED is committed without a durably-queued REJECTED when a
        valid route exists, and no queued message is written without the local
        transition (a failed transition rolls back the uncommitted enqueue).

        Idempotent at the durable layer: the outbox dedup (`mca_outgoing_replies`
        UNIQUE(attachment_id, event_type)) makes a repeat call a no-op on the
        message, and the WAITING_CONSENT state guard makes it a no-op on the
        transition. Radio unavailability does **not** undo the local rejection -
        the frame stays PENDING in the outbox and is dispatched later with
        backoff, surviving restart. The command reports success once the local
        transition and the enqueue are durably committed; it does not wait for
        radio delivery."""
        attachment_id = command.payload.get("attachment_id")
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT id, direction, state, transfer_id FROM attachments "
            "WHERE id = ? AND workspace_id = ?",
            (attachment_id, self._principal.workspace_id),
        ).fetchone()
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        if row["direction"] != "received" or row["state"] != receiver.WAITING_CONSENT:
            return CommandOutcome.failed("invalid_state_transition")
        try:
            with self._atomic("reject"):
                receiver.reject(self._conn, attachment_id, now=self._now(), commit=False)
                receiver.enqueue_control_message(
                    self._conn,
                    attachment_id=attachment_id,
                    transfer_id=bytes.fromhex(row["transfer_id"]),
                    message_type=codec.MessageType.REJECTED,
                    event_type=receiver._EVENT_REJECTED_SENT,
                    principal=self._principal,
                    workspace_manager=self._workspace_manager,
                    now=self._now(),
                )
        except receiver.ReceiverError:
            # A race (the row moved off WAITING_CONSENT between read and call)
            # - the SAVEPOINT rolled back the uncommitted transition + enqueue,
            # and the state guard reports the same failure the old delegate did.
            self._rollback_silently()
            return CommandOutcome.failed("invalid_state_transition")
        except ValueError:
            # A malformed transfer_id - same rollback, same report.
            self._rollback_silently()
            return CommandOutcome.failed("invalid_state_transition")
        except Exception as exc:  # noqa: BLE001 - never leak a partial transition
            # PR 2.5 correction: a signing-key load / codec / unexpected failure
            # inside enqueue_control_message() after the state was already
            # written must still roll back the whole operation and report a
            # terminal internal failure - not leave an open transaction that a
            # later commit would half-apply. Sanitized: class name only.
            self._rollback_silently()
            logger.error(
                "AttachmentsService: command %s (%s) handler raised %s",
                command.command_id, command.kind, type(exc).__name__,
            )
            return CommandOutcome.failed(COMMAND_EXECUTION_FAILED)
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "state": receiver.REJECTED},
        )

    def _cancel_row(self, attachment_id: str) -> Optional[sqlite3.Row]:
        """The wider persisted row a cancel needs beyond `_attachment_row`:
        `transfer_id` (the Relay object to revoke) and `saved_path` (cleared,
        never used as a filesystem component). Re-read from the worker-owned
        `conn` (§3.1). Returns `None` for an unknown id or a row in another
        workspace."""
        self._conn.row_factory = sqlite3.Row
        return self._conn.execute(
            "SELECT id, direction, provider_id, state, transfer_id, saved_path FROM attachments "
            "WHERE id = ? AND workspace_id = ?",
            (attachment_id, self._principal.workspace_id),
        ).fetchone()

    def _sender_state_row(self, attachment_id: str) -> Optional[sqlite3.Row]:
        """The sender-state columns a cancel consults to decide whether a
        Relay object exists to revoke (`upload_id`/`revoke_token`). Only the
        two remote-cleanup signals are projected - the data key / nonce /
        upload token stay out of a handler that must never touch key material
        it has no use for."""
        self._conn.row_factory = sqlite3.Row
        return self._conn.execute(
            "SELECT upload_id, revoke_token FROM mca_sender_state WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchone()

    def _revoke_token_for(self, attachment_id: str) -> Optional[str]:
        """ADR-0009 Decision 5a: the revoke token a revoke needs, read from the
        retained `mca_sender_revoke_state` first - the row that survives
        ACK_DOWNLOADED's deletion of `mca_sender_state` - falling back to
        `mca_sender_state` only for a row that predates the migration (or is
        still pre-DOWNLOADED, where the transient row still holds the token)."""
        self._conn.row_factory = sqlite3.Row
        row = self._conn.execute(
            "SELECT revoke_token FROM mca_sender_revoke_state WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()
        if row is not None and row["revoke_token"] is not None:
            return row["revoke_token"]
        row = self._conn.execute(
            "SELECT revoke_token FROM mca_sender_state WHERE attachment_id = ?", (attachment_id,)
        ).fetchone()
        return row["revoke_token"] if row is not None else None

    def _cancel_route_for(self, attachment_id: str) -> Optional["receiver.ReplyRoute"]:
        """PR 2.5: the immutable route snapshot for a sender-side CANCEL, read
        from `attachment_deliveries` (the same row `_step_ready_to_send` builds
        its OFFER `Route` from). `attachment_deliveries` has no
        `destination_address` column, so for the DIRECT-only MVP it is set equal
        to `route_id` - the same conflation `_step_ready_to_send` already makes.
        Returns None when the delivery row is missing or any route field is
        NULL. PR 2.5 correction: that None is passed through to
        `enqueue_control_message(route=None)` as an *explicitly incomplete*
        snapshot - the sender-side CANCEL must never fall back to the
        attachment's own `reply_*` route (a sender row's reply_* columns are
        unrelated to where its CANCEL goes; falling back would hand a CANCEL to
        a wholly wrong address). The dispatch step then fails it closed to
        UNDELIVERABLE rather than guessing a destination."""
        self._conn.row_factory = sqlite3.Row
        delivery = self._conn.execute(
            "SELECT adapter_id, connector_profile_id, route_type, route_id "
            "FROM attachment_deliveries WHERE attachment_id = ? ORDER BY id LIMIT 1",
            (attachment_id,),
        ).fetchone()
        if delivery is None:
            return None
        fields = (
            delivery["adapter_id"],
            delivery["connector_profile_id"],
            delivery["route_type"],
            delivery["route_id"],
        )
        if any(field is None for field in fields):
            return None
        return receiver.ReplyRoute(
            adapter_id=delivery["adapter_id"],
            connector_profile_id=delivery["connector_profile_id"],
            route_type=delivery["route_type"],
            route_id=delivery["route_id"],
            destination_address=delivery["route_id"],
        )

    def _command_cancel(self, command: Command) -> CommandOutcome:
        """`attachment_cancel` (§7.4): the explicit user action that cancels
        an outgoing attachment still in an automatic (pre-SENT) state. The
        cancel has a remote half and a local half, and they run strictly in
        that order:

        - **remote first**: if a Relay upload session was ever created
          (`upload_id` or `revoke_token` persisted), `RelayClient.revoke()` is
          called against the provider pinned on the row (never a default). A
          Relay **404 is confirmed absence** - the object no longer exists
          remotely, so the remote half is already satisfied - whereas every
          other failure (`RelayUnavailableError`/`RelayHTTPError`/missing or
          disabled provider) is `relay_unreachable`, leaving the row, its
          sender state, and its spool file all *preserved* for a manual retry
          (no auto retry - a committed-but-unreachable object must never be
          silently abandoned).
        - **local second, only after the remote half is resolved**: delete the
          staged spool (`spool/outgoing/<attachment_id>`, derived from the
          validated id only - never `saved_path`, which is cleared to NULL,
          never handed to the filesystem), then `sender.cancel()` in the same
          transaction. An unlink failure is `spool_cleanup_failed` with the
          row unchanged.

        Result (state=CANCELLED, saved_path NULL, no sender-state row, no
        spool file, history preserved) is only reached when both halves
        completed."""
        attachment_id = command.payload.get("attachment_id")
        row = self._cancel_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        if row["direction"] != "sent" or row["state"] not in sender.AUTOMATIC_STATES:
            return CommandOutcome.failed("invalid_state_transition")

        sender_state = self._sender_state_row(attachment_id)
        upload_id = sender_state["upload_id"] if sender_state is not None else None
        revoke_token = sender_state["revoke_token"] if sender_state is not None else None
        needs_remote = upload_id is not None or revoke_token is not None
        if needs_remote:
            if revoke_token is None:
                # A remote session exists but its revoke token was never
                # persisted - we cannot revoke it, so we cannot complete a
                # safe cancel. Preserve everything for a manual retry.
                return CommandOutcome.failed("relay_unreachable")
            provider_id_text = _provider_id_text(row["direction"], row["provider_id"])
            profile = self._provider_registry.resolve(provider_id_text) if provider_id_text else None
            if profile is None or not profile.enabled:
                return CommandOutcome.failed("relay_unreachable")
            relay_client = self._relay_client_factory(provider_id_text) if provider_id_text else None
            if relay_client is None:
                return CommandOutcome.failed("relay_unreachable")
            try:
                relay_client.revoke(bytes.fromhex(row["transfer_id"]), revoke_token)
            except RelayHTTPError as exc:
                if exc.status_code != 404:
                    return CommandOutcome.failed("relay_unreachable")
                # 404 = confirmed absence: the remote half is already done.
            except RelayError:
                return CommandOutcome.failed("relay_unreachable")

        spool_path = self._spool_path_for(attachment_id)
        if spool_path is not None:
            try:
                spool_path.unlink(missing_ok=True)
            except OSError:
                return CommandOutcome.failed("spool_cleanup_failed")
        self._conn.execute("UPDATE attachments SET saved_path = NULL WHERE id = ?", (attachment_id,))
        try:
            sender.cancel(self._conn, attachment_id, now=self._now())
        except sender.SenderError:
            self._rollback_silently()
            return CommandOutcome.failed("invalid_state_transition")
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "state": sender.CANCELLED},
        )

    # ---- Step 1.6A.5 content/save/revoke/local-content commands -------------

    def _content_row(self, attachment_id: str) -> Optional[sqlite3.Row]:
        """The wider persisted row the content-affecting commands (save /
        local-content) need beyond `_attachment_row`: `file_name` (the display
        name, sanitized only at its use points - §7.3/§7.14), `mime_type`/
        `plain_size`, and `saved_path` (the descriptor's source -
        `cache/incoming/<id>` before a save, `files/` after one). Re-read from
        the worker-owned `conn` (§3.1). Returns `None` for an unknown id or a
        row in another workspace."""
        self._conn.row_factory = sqlite3.Row
        return self._conn.execute(
            "SELECT id, direction, provider_id, state, file_name, mime_type, plain_size, saved_path "
            "FROM attachments WHERE id = ? AND workspace_id = ?",
            (attachment_id, self._principal.workspace_id),
        ).fetchone()

    def _resolve_content_path(self, paths, saved_path) -> Optional[Path]:
        """Resolve a persisted `saved_path` back to an absolute, serve-time
        re-validated content file path (`make_locator` + `resolve_locator`),
        or `None` if the path is not a servable content file (a sent row's
        spool path, a NULL/malformed/traversing path, or one that resolves
        outside `files/`/`cache/incoming/` - `resolve_locator` follows symlinks
        and rejects escapes). The one filesystem-touching step §7.14 allows;
        never reads `conn`."""
        locator = make_locator(paths, saved_path)
        if locator is None:
            return None
        try:
            return resolve_locator(paths, locator)
        except ContentLocatorError:
            return None

    def _command_save(self, command: Command) -> CommandOutcome:
        """`attachment_save` (§7.3): move a received `AVAILABLE` attachment's
        verified plaintext out of `cache/incoming/<id>` and into `files/`
        under a collision-resolving safe display name (`unique_file_name()`),
        flipping `saved` true. Genuinely idempotent, not merely deduplicated
        (§7.3 note): `unique_file_name()` resolves a name conflict only on the
        **first** save - a re-save of an already-`saved` row returns the prior
        result (`saved=true`, same `file_name`) **without copying** when the
        `files/` copy still exists, and `content_missing` when it has since
        been deleted. Never overwrites an existing `files/` name.

        The move is a same-filesystem `Path.replace()` (atomic), done before
        the `saved_path` UPDATE commits, so a failed move leaves the row (and
        its cache copy) untouched. The `file_name` is sanitized
        (`sanitize_display_name` - basename, printable-only, NUL/separator/
        dot-stripped) and capped to `MAX_SOURCE_NAME_CODE_POINTS` before it
        ever becomes a `files/` component - never trusted as a path. The
        snapshot is refreshed before the success is reported, so the client's
        next GET already sees `saved=true`."""
        attachment_id = command.payload.get("attachment_id")
        row = self._content_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        if row["direction"] != "received" or row["state"] != receiver.AVAILABLE:
            return CommandOutcome.failed("invalid_state_transition")

        paths = self._workspace_manager.paths(self._principal.principal_id)
        saved_path = row["saved_path"]
        saved = make_locator(paths, saved_path) is not None and _is_inside_files(paths, saved_path)

        if saved:
            # Idempotent re-save: the row already points into files/. Return
            # the prior result without copying; fail only if the copy is gone.
            existing = self._resolve_content_path(paths, saved_path)
            if existing is None or not existing.is_file():
                return CommandOutcome.failed("content_missing")
            return CommandOutcome.succeeded(
                resource_id=attachment_id,
                result={"attachment_id": attachment_id, "saved": True, "file_name": row["file_name"]},
            )

        source = self._resolve_content_path(paths, saved_path)
        if source is None or not source.is_file():
            return CommandOutcome.failed("content_missing")

        safe_name = (
            sanitize_display_name(row["file_name"])[:MAX_SOURCE_NAME_CODE_POINTS].strip(".")
            or "attachment"
        )
        dest = self._workspace_manager.unique_file_name(self._principal.principal_id, safe_name)
        source.replace(dest)
        self._conn.execute(
            "UPDATE attachments SET saved_path = ? WHERE id = ?", (str(dest), attachment_id)
        )
        self._conn.commit()
        self._refresh_snapshot()
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "saved": True, "file_name": row["file_name"]},
        )

    def _command_revoke(self, command: Command) -> CommandOutcome:
        """`attachment_revoke` (§7.3/§7.4/§8): revoke a sent attachment's Relay
        object and mark it REVOKED. Remote-first, exactly like cancel's remote
        half, so a committed-but-unreachable object is never marked REVOKED on
        an unconfirmed remote failure:

        - **remote first**: the Relay object identified by the row's persisted
          `transfer_id` is revoked with the persisted `revoke_token`. A Relay
          **404 is confirmed absence** (already gone → the remote half is
          satisfied); any other failure, or a missing `transfer_id`/
          `revoke_token`/pinned provider, is `relay_unreachable` with the row
          and sender state preserved for a manual retry.
        - **local second, only after the remote half resolves**: `sender.revoke()`
          (SENT/RECEIVED/DOWNLOADED → REVOKED, `mca_sender_state` dropped).

        PR 2.5 adds the outbound CANCEL to the local half, but only for
        **SENT** and **RECEIVED** (the receiver has not downloaded the plaintext,
        so a radio CANCEL can still retract the offer): the `REVOKED` transition
        and the signed CANCEL enqueue share **one** DB transaction. For
        **DOWNLOADED** the local Relay revoke + `REVOKED` transition still
        complete, but **no** radio CANCEL is generated - a CANCEL cannot retract
        plaintext the receiver already holds, so sending one would be a lie (it
        would only tell the receiver to discard an object it already has). A
        Relay revoke that fails leaves the state unchanged and enqueues nothing
        (the revoke capability is preserved for a retry).

        Reuses `_cancel_row` (transfer_id + pinned provider_id) - revoke has no
        spool to clean, so there is no local filesystem step, only the state
        transition. The snapshot is refreshed before the success is reported."""
        attachment_id = command.payload.get("attachment_id")
        row = self._cancel_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        if row["direction"] != "sent" or row["state"] not in (
            sender.SENT,
            sender.RECEIVED,
            sender.DOWNLOADED,
        ):
            return CommandOutcome.failed("invalid_state_transition")

        revoke_token = self._revoke_token_for(attachment_id)
        transfer_id = row["transfer_id"]
        if transfer_id is None or revoke_token is None:
            # A Relay object exists but its revoke token (or transfer id) was
            # never persisted - we cannot revoke it safely. Preserve everything.
            return CommandOutcome.failed("relay_unreachable")
        provider_id_text = _provider_id_text(row["direction"], row["provider_id"])
        profile = self._provider_registry.resolve(provider_id_text) if provider_id_text else None
        if profile is None or not profile.enabled:
            return CommandOutcome.failed("relay_unreachable")
        relay_client = self._relay_client_factory(provider_id_text) if provider_id_text else None
        if relay_client is None:
            return CommandOutcome.failed("relay_unreachable")
        try:
            relay_client.revoke(bytes.fromhex(transfer_id), revoke_token)
        except RelayHTTPError as exc:
            if exc.status_code != 404:
                return CommandOutcome.failed("relay_unreachable")
            # 404 = confirmed absence: the remote half is already done.
        except RelayError:
            return CommandOutcome.failed("relay_unreachable")

        # PR 2.5: CANCEL is generated only for SENT/RECEIVED (the receiver has
        # not yet downloaded the plaintext). DOWNLOADED still transitions to
        # REVOKED locally but sends no radio CANCEL.
        should_send_cancel = row["state"] in (sender.SENT, sender.RECEIVED)
        cancel_route = self._cancel_route_for(attachment_id) if should_send_cancel else None
        try:
            with self._atomic("revoke"):
                sender.revoke(self._conn, attachment_id, now=self._now(), commit=False)
                if should_send_cancel:
                    receiver.enqueue_control_message(
                        self._conn,
                        attachment_id=attachment_id,
                        transfer_id=bytes.fromhex(transfer_id),
                        message_type=codec.MessageType.CANCEL,
                        event_type=sender.CANCEL_EVENT_TYPE,
                        principal=self._principal,
                        workspace_manager=self._workspace_manager,
                        now=self._now(),
                        route=cancel_route,
                    )
        except sender.SenderError:
            # A raced state change - the SAVEPOINT rolled back the uncommitted
            # transition + enqueue so the revoke capability and prior state are
            # preserved for a retry.
            self._rollback_silently()
            return CommandOutcome.failed("invalid_state_transition")
        except ValueError:
            # A malformed transfer_id - same rollback, same report.
            self._rollback_silently()
            return CommandOutcome.failed("invalid_state_transition")
        except Exception as exc:  # noqa: BLE001 - never leak a partial transition
            # PR 2.5 correction: a signing-key load / codec / unexpected failure
            # after the state was already written must still roll back the whole
            # operation and report a terminal internal failure. Sanitized.
            self._rollback_silently()
            logger.error(
                "AttachmentsService: command %s (%s) handler raised %s",
                command.command_id, command.kind, type(exc).__name__,
            )
            return CommandOutcome.failed(COMMAND_EXECUTION_FAILED)
        self._refresh_snapshot()
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "state": sender.REVOKED},
        )

    def _command_delete_local_content(self, command: Command) -> CommandOutcome:
        """`attachment_delete_local_content` (§7.3/§8): delete the `files/` copy
        of a saved attachment, flipping `saved` false while keeping the
        attachment row and its full history intact. Precondition is `saved=true`
        (§7.3) - derived here, never trusted from the snapshot, so a row that
        raced to `saved=false` (or was never saved) fails `not_saved` rather
        than deleting something it does not have. The `files/` file is unlinked
        first (`missing_ok` - an already-gone copy is clean), then `saved_path`
        is cleared to NULL and committed, so `saved` can never report false
        while the file still exists on disk (an unlink failure leaves the row
        unchanged). The snapshot is refreshed before the success is reported."""
        attachment_id = command.payload.get("attachment_id")
        row = self._content_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")

        paths = self._workspace_manager.paths(self._principal.principal_id)
        saved_path = row["saved_path"]
        saved = make_locator(paths, saved_path) is not None and _is_inside_files(paths, saved_path)
        if not saved:
            return CommandOutcome.failed("not_saved")

        path = self._resolve_content_path(paths, saved_path)
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("AttachmentsService: could not unlink a saved files/ copy")
                return CommandOutcome.failed("content_missing")
        self._conn.execute(
            "UPDATE attachments SET saved_path = NULL WHERE id = ?", (attachment_id,)
        )
        self._conn.commit()
        self._refresh_snapshot()
        return CommandOutcome.succeeded(
            resource_id=attachment_id,
            result={"attachment_id": attachment_id, "saved": False},
        )

    def _auto_request_missing_keys(self) -> None:
        """PR 1 (recoverable missing-key workflow): drive the missing-key
        half of the receiver's WAITING_KEY state forward without a human
        having to click "request key" for every transfer. For each received
        attachment parked in WAITING_KEY whose signer's key is STILL unknown,
        send a rate-limited KEY_REQUEST to the persisted reply route - the
        same per-address interval and persisted `last_request_sent_at`
        `_command_request_key` uses, plus a persisted workspace-wide hourly
        budget - so the sender is prompted to announce its key and the
        transfer can later resume once the human confirms it.

        PR 1 correction pass: three layered safeguards keep a stranger
        flooding OFFERs from many distinct unknown-key addresses from
        turning this scan into an unbounded outbound burst:

        - **per-tick send cap** (`MAX_AUTO_KEY_REQUESTS_PER_TICK`), the same
          shape as `MAX_ATTACHMENTS_PER_TICK`;
        - **per-canonical-address dedup within the tick**: `attempted` holds
          every address already tried this tick *including failed/unsent*
          sends, so two rows from the same sender can never both send in one
          tick;
        - **persisted workspace-wide hourly budget** (migration 15), checked
          per-row so mid-scan exhaustion stops the scan for the rest of the
          tick.

        Mirrors `_command_request_key`'s revalidation posture: nothing about
        a parked row is trusted here either. Safe to run every tick: the
        per-address rate limit collapses repeated attempts to one request
        per window, a row already advanced out of WAITING_KEY this tick is
        simply not re-scanned, and one bad row must not stop the scan."""
        if self._delivery_adapter is None:
            return
        self._conn.row_factory = sqlite3.Row
        # `ORDER BY created_at` (oldest first) so a flood of parked offers is
        # serviced fairly rather than in arbitrary SQLite row order; the send
        # cap below (not a scan LIMIT) bounds the outbound burst without
        # letting an already-rate-limited row starve a not-yet-requested one.
        rows = self._conn.execute(
            "SELECT id, reply_adapter_id, reply_connector_profile_id, reply_route_id, reply_route_type, "
            "reply_destination_address, pending_offer_cbor, hard_expires_at "
            "FROM attachments WHERE workspace_id = ? AND direction = 'received' AND state = ? ORDER BY created_at",
            (self._principal.workspace_id, receiver.WAITING_KEY),
        ).fetchall()
        sent_this_tick = 0
        attempted: "set[str]" = set()
        for row in rows:
            if sent_this_tick >= MAX_AUTO_KEY_REQUESTS_PER_TICK:
                break
            try:
                outcome = self._auto_request_key_for_row(row, attempted)
            except Exception:  # noqa: BLE001 - one bad row must not stop the scan
                logger.exception(
                    "AttachmentsService: auto key-request failed for attachment %s", row["id"]
                )
                continue
            if outcome == _AUTO_KEY_REQUEST_QUOTA_EXHAUSTED:
                # Workspace-wide hourly budget is full - no further automatic
                # request can be sent this tick regardless of row, so stop.
                break
            if outcome == _AUTO_KEY_REQUEST_SENT:
                sent_this_tick += 1

    def _auto_request_key_for_row(self, row: sqlite3.Row, attempted: "set[str]") -> Optional[str]:
        route_id = row["reply_route_id"]
        route_type = row["reply_route_type"]
        destination = row["reply_destination_address"]
        pending_raw = row["pending_offer_cbor"]
        if not route_id or pending_raw is None:
            return None
        # PR 1 (final correction): do not automatically request a key for an
        # expired WAITING_KEY transfer. `hard_expires_at` is the receiver's own
        # persisted deadline; once it has passed (<= now, boundary included)
        # there is nothing to resume, so the row is skipped outright rather
        # than pinging the sender for a key to a dead transfer. (PR 2 will own
        # the actual EXPIRED state transition; this scan simply must not keep
        # requesting keys for a row past its deadline.)
        hard_expires_at = row["hard_expires_at"]
        if hard_expires_at is not None and hard_expires_at <= self._now():
            return None
        # PR 1 (correction): validate the *complete* persisted reply route
        # before sending, not just the bare `reply_route_id`. Mirroring
        # `_dispatch_outgoing_replies`'s ACK-outbox posture, the persisted
        # `reply_adapter_id`/`reply_connector_profile_id` must exactly match
        # this service's own `delivery_adapter` (a NULL or mismatched value
        # fails closed, same as a wholly-missing route - never guessed at),
        # the reply is DIRECT-only, and the destination must equal the
        # (already-canonical) route id. A malformed, mismatched, or
        # non-DIRECT persisted route is never sent to (left for the human's
        # manual action).
        adapter_id = self._delivery_adapter.adapter_id
        connector_profile_id = self._delivery_adapter.connector_profile_id
        if (
            route_type != "DIRECT"
            or not _is_contact_id(route_id)
            or destination != route_id
            or row["reply_adapter_id"] != adapter_id
            or row["reply_connector_profile_id"] != connector_profile_id
        ):
            logger.warning(
                "AttachmentsService: skipping auto key-request for attachment %s: "
                "invalid persisted reply route (type=%r, id=%r, destination=%r, "
                "adapter=%r, connector=%r)",
                row["id"], route_type, route_id, destination,
                row["reply_adapter_id"], row["reply_connector_profile_id"],
            )
            return None
        # Per-canonical-address dedup within this tick: `attempted` covers
        # sent, failed, and rate-limited attempts alike, so a second row from
        # the same sender never re-sends in the same tick. (`route_id` is
        # already canonical - validated at the inbound boundary - so this key
        # needs no `.lower()` normalization.)
        if route_id in attempted:
            return None
        attempted.add(route_id)
        try:
            unverified = codec.decode_offer(bytes(pending_raw), verify_key=None)
        except codec.CodecError:
            return None
        if self._key_exchange.get_binding_by_key_id(unverified.sender_key_id.hex()) is not None:
            # Key is now known (announced since the OFFER parked it) - the
            # trust gate in `_step_waiting_key` owns whether/when to resume.
            return None
        try:
            self._key_exchange.check_key_request_rate_limit(route_id, self._now())
        except RateLimited:
            return None
        try:
            self._key_exchange.check_auto_key_request_quota(self._now())
        except RateLimited:
            return _AUTO_KEY_REQUEST_QUOTA_EXHAUSTED
        key_request = self._key_exchange.build_key_request()
        route = Route(route_type=RouteType.DIRECT, route_id=route_id, destination_address=route_id)
        try:
            wire_payload = self._delivery_adapter.encode(key_request, route)
            receipt = self._delivery_adapter.send(
                wire_payload, route, idempotency_key=f"auto-key-request-{row['id']}"
            )
        except DeliveryError:
            return None
        if not receipt.sent:
            return None
        self._key_exchange.record_key_request_sent(route_id, self._now())
        self._key_exchange.record_auto_key_request_sent(self._now())
        return _AUTO_KEY_REQUEST_SENT

    def _command_request_key(self, command: Command) -> CommandOutcome:
        """`contact_request_key` (§7.10): send a signed KEY_REQUEST to a
        contact whose key is unknown/unverified/changed, over the fixed
        DIRECT route (route_id == destination_address == the contact
        transport address). Re-validates contact id/adapter/route (never
        trusts the queue), re-reads the live binding (a contact that became
        `MCA_READY` since the request thread's snapshot read fails with
        `key_already_known`), checks the persisted outgoing key-request rate
        limit, then encodes and sends. The rate-limit timestamp is persisted
        *only* after the delivery receipt reports `sent == True` - a failed
        send never consumes the quota. Any delivery failure (DeliveryError,
        a false receipt, or a missing delivery adapter) is `radio_unavailable`
        and consumes no quota."""
        contact_id = command.payload.get("contact_id")
        adapter_id = command.payload.get("adapter_id")
        route_id = command.payload.get("route_id")
        if not _is_contact_id(contact_id):
            return CommandOutcome.failed("invalid_contact_id")
        if adapter_id != "meshtastic" or route_id != contact_id:
            return CommandOutcome.failed("invalid_contact_id")

        if self._key_exchange.get_status(contact_id) is AddressStatus.MCA_READY:
            return CommandOutcome.failed("key_already_known")
        try:
            self._key_exchange.check_key_request_rate_limit(contact_id, self._now())
        except RateLimited:
            return CommandOutcome.failed("rate_limited")

        if self._delivery_adapter is None:
            return CommandOutcome.failed("radio_unavailable")
        key_request = self._key_exchange.build_key_request()
        route = Route(route_type=RouteType.DIRECT, route_id=contact_id, destination_address=contact_id)
        try:
            wire_payload = self._delivery_adapter.encode(key_request, route)
            receipt = self._delivery_adapter.send(wire_payload, route, idempotency_key=command.command_id)
        except DeliveryError:
            return CommandOutcome.failed("radio_unavailable")
        if not receipt.sent:
            return CommandOutcome.failed("radio_unavailable")

        self._key_exchange.record_key_request_sent(contact_id, self._now())
        return CommandOutcome.succeeded(
            resource_id=contact_id,
            result={"contact_id": contact_id, "status": "requested"},
        )

    # ---- Step 1.7: contact trust (Files workspace) -----------------------

    def _command_confirm(self, command: Command) -> CommandOutcome:
        """`contact_confirm` (Step 1.7): set `tofu_confirmed_at` on a contact
        whose key is announced but never confirmed (`KEY_UNVERIFIED`). The
        worker re-reads the live binding (never trusts the queue): no binding
        -> `contact_not_found`, any status other than `KEY_UNVERIFIED` ->
        `invalid_state_transition`. On success the recipient snapshot is
        refreshed so `GET /api/mca/contacts` reflects `trusted`."""
        contact_id = command.payload.get("contact_id")
        adapter_id = command.payload.get("adapter_id")
        if not _is_contact_id(contact_id) or adapter_id != "meshtastic":
            return CommandOutcome.failed("invalid_contact_id")
        binding = self._key_exchange.get_binding(contact_id)
        if binding is None:
            return CommandOutcome.failed("contact_not_found")
        if binding.status is not AddressStatus.KEY_UNVERIFIED:
            return CommandOutcome.failed("invalid_state_transition")
        contacts.confirm_binding(self._key_exchange, contact_id, now=self._now())
        self._refresh_recipient_snapshot()
        return CommandOutcome.succeeded(
            resource_id=contact_id,
            result={"contact_id": contact_id, "status": "trusted"},
        )

    def _command_accept_key_change(self, command: Command) -> CommandOutcome:
        """`contact_accept_key_change` (Step 1.7): promote a parked key change
        into the trusted binding. The worker re-reads the live binding (never
        trusts the queue): no binding -> `contact_not_found`, not `KEY_CHANGED`
        -> `invalid_state_transition`. Accepting resets `tofu_confirmed_at`, so
        the promoted key lands back in `KEY_UNVERIFIED` awaiting its own,
        separate confirm. On success the recipient snapshot is refreshed."""
        contact_id = command.payload.get("contact_id")
        adapter_id = command.payload.get("adapter_id")
        if not _is_contact_id(contact_id) or adapter_id != "meshtastic":
            return CommandOutcome.failed("invalid_contact_id")
        binding = self._key_exchange.get_binding(contact_id)
        if binding is None:
            return CommandOutcome.failed("contact_not_found")
        if binding.status is not AddressStatus.KEY_CHANGED:
            return CommandOutcome.failed("invalid_state_transition")
        try:
            contacts.accept_key_change(self._key_exchange, contact_id, now=self._now())
        except contacts.ContactError:
            return CommandOutcome.failed("invalid_state_transition")
        self._refresh_recipient_snapshot()
        return CommandOutcome.succeeded(
            resource_id=contact_id,
            result={"contact_id": contact_id, "status": "confirmation_required"},
        )

    def _command_reject_key_change(self, command: Command) -> CommandOutcome:
        """`contact_reject_key_change` (Step 1.7): dismiss a parked key change,
        leaving the existing trusted binding untouched (not a blacklist). The
        worker re-reads the live binding (never trusts the queue): no binding
        -> `contact_not_found`, not `KEY_CHANGED` -> `invalid_state_transition`.
        On success the recipient snapshot is refreshed."""
        contact_id = command.payload.get("contact_id")
        adapter_id = command.payload.get("adapter_id")
        if not _is_contact_id(contact_id) or adapter_id != "meshtastic":
            return CommandOutcome.failed("invalid_contact_id")
        binding = self._key_exchange.get_binding(contact_id)
        if binding is None:
            return CommandOutcome.failed("contact_not_found")
        if binding.status is not AddressStatus.KEY_CHANGED:
            return CommandOutcome.failed("invalid_state_transition")
        try:
            contacts.reject_key_change(self._key_exchange, contact_id)
        except contacts.ContactError:
            return CommandOutcome.failed("invalid_state_transition")
        self._refresh_recipient_snapshot()
        return CommandOutcome.succeeded(
            resource_id=contact_id,
            result={"contact_id": contact_id, "status": "trusted"},
        )

    # ---- Step 1.6A.4: multi-Relay provider onboarding/management --------

    def _command_provider_probe(self, command: Command) -> CommandOutcome:
        """`provider_probe` (§7.11 phase 1): resolve the browser-supplied
        origin (defensive re-validation of what the request thread already
        normalized - never trusts the queue), reach the Relay over a §12
        SSRF-pinned `RelayClient`, and record its `/v1/info` as a single-use
        `ProbeRecord`. The worker is the sole executor of DNS/HTTP here, so
        no request thread ever touches the network. Nothing is persisted to
        SQLite - the probe record is in-memory and TTL-bounded (§3.2)."""
        base_url = command.payload.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            return CommandOutcome.failed("invalid_origin")
        try:
            origin = normalize_origin(base_url)
        except ProviderRegistryError:
            return CommandOutcome.failed("invalid_origin")

        # `RelayClient(origin)` eagerly runs the §12 policy (HTTPS-only,
        # cert-verify-on, DNS resolve + global-routability check + IP pinning,
        # redirects off) via `build_secure_session`, so construction itself
        # surfaces an unroutable/non-HTTPS origin.
        try:
            client = RelayClient(origin)
        except RelayNetworkError as exc:
            return CommandOutcome.failed(exc.error_code)
        try:
            info = client.get_info()
        except RelayNetworkError as exc:
            return CommandOutcome.failed(exc.error_code)
        except (RelayError, requests.RequestException):
            return CommandOutcome.failed("relay_unreachable")

        provider_id = compute_provider_id(origin, info.service_public_key)
        if provider_id != info.provider_id:
            return CommandOutcome.failed("relay_identity_mismatch")

        service_key_fingerprint = hashlib.sha256(info.service_public_key).hexdigest()
        probe_id = mint_probe_id()
        record = ProbeRecord(
            probe_id=probe_id,
            origin=origin,
            provider_id=provider_id,
            service_public_key=info.service_public_key,
            service_key_fingerprint=service_key_fingerprint,
            protocol_version=None,
            max_ciphertext_bytes=info.limits.max_ciphertext_bytes,
            min_ttl_seconds=None,
            max_ttl_seconds=info.limits.max_hard_ttl_seconds,
            expires_at=self._now() + PROBE_TTL_SECONDS,
            status=PROBE_STATUS_PROBED,
        )
        self._probe_registry.add(record)
        return CommandOutcome.succeeded(
            resource_id=probe_id, result=serialize_probe_record(record)
        )

    def _command_provider_register(self, command: Command) -> CommandOutcome:
        """`provider_register` (§7.11 phase 2): consume the single-use probe
        and materialize the profile from the probe's own server-fetched
        identity fields - never from browser-supplied Relay parameters
        (`origin`/`service_public_key`/TTLs/`protocol_version`/derived
        `provider_id` all come from the `ProbeRecord`). The browser chooses
        only the policy fields (`kind`/`upload_allowed`/`download_allowed`/
        `max_ciphertext_bytes`) and confirms the fingerprint. First
        registration becomes the default; later ones never silently replace
        it (§7.12 - explicit `set_default()` only)."""
        probe_id = command.payload.get("probe_id")
        if not isinstance(probe_id, str) or not probe_id:
            return CommandOutcome.failed("probe_id_not_found")
        probe = self._probe_registry.consume(probe_id)
        if probe is None:
            # Single-use (§7.11): None here is a replay (already consumed) or
            # a probe that expired after the request thread's synchronous
            # validation but before this execution - either way, re-probe.
            return CommandOutcome.failed("probe_id_used")

        if command.payload.get("fingerprint_confirmation") != probe.service_key_fingerprint:
            return CommandOutcome.failed("provider_id_mismatch")

        policy = command.payload.get("policy")
        if not isinstance(policy, dict):
            policy = {}
        kind = policy.get("kind", "own")
        upload_allowed = policy.get("upload_allowed", True)
        download_allowed = policy.get("download_allowed", True)
        if not isinstance(kind, str):
            return CommandOutcome.failed("invalid_metadata")
        if not isinstance(upload_allowed, bool) or not isinstance(download_allowed, bool):
            return CommandOutcome.failed("invalid_metadata")

        max_ciphertext_bytes = policy.get("max_ciphertext_bytes")
        if max_ciphertext_bytes is None:
            max_ciphertext_bytes = probe.max_ciphertext_bytes
        elif (
            isinstance(max_ciphertext_bytes, bool)
            or not isinstance(max_ciphertext_bytes, int)
            or max_ciphertext_bytes <= 0
            or max_ciphertext_bytes > probe.max_ciphertext_bytes
        ):
            # §7.11: a supplied max_ciphertext_bytes must not exceed the
            # probe's advertised limit (synchronous `invalid_metadata`).
            return CommandOutcome.failed("invalid_metadata")

        try:
            profile = self._provider_registry.register(
                display_name=command.payload.get("display_name"),
                base_url=probe.origin,
                service_public_key=probe.service_public_key,
                max_ciphertext_bytes=max_ciphertext_bytes,
                upload_allowed=upload_allowed,
                download_allowed=download_allowed,
                kind=kind,
                min_ttl_seconds=probe.min_ttl_seconds,
                max_ttl_seconds=probe.max_ttl_seconds,
                protocol_version=probe.protocol_version,
                now=self._now(),
            )
        except ProviderRegistryError as exc:
            return CommandOutcome.failed(exc.error_code or "invalid_metadata")

        # First registered becomes default; never silently replaces an
        # existing default (§7.12).
        if self._provider_registry.get_default() is None:
            self._provider_registry.set_default(profile.provider_id)

        return CommandOutcome.succeeded(
            resource_id=profile.provider_id, result={"provider_id": profile.provider_id}
        )

    def _command_provider_update(self, command: Command) -> CommandOutcome:
        """`provider_update` (§7.12): partial edit of non-identity fields.
        Identity fields (`origin`/`service_public_key`) are not editable by
        design - `update_profile()` refuses them and there is no payload key
        for them. A CLEAR sentinel (produced by the API layer from JSON
        `null` + `clear: true`) clears a TTL/`protocol_version` field;
        `None`/absent leaves it alone."""
        provider_id = command.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return CommandOutcome.failed("invalid_provider_id")
        if self._provider_registry.resolve(provider_id) is None:
            return CommandOutcome.failed("provider_not_found")

        for value in (
            command.payload.get("enabled"),
            command.payload.get("upload_allowed"),
            command.payload.get("download_allowed"),
        ):
            if value is not None and not isinstance(value, bool):
                return CommandOutcome.failed("invalid_metadata")

        try:
            profile = self._provider_registry.update_profile(
                provider_id,
                display_name=command.payload.get("display_name"),
                enabled=command.payload.get("enabled"),
                upload_allowed=command.payload.get("upload_allowed"),
                download_allowed=command.payload.get("download_allowed"),
                min_ttl_seconds=command.payload.get("min_ttl_seconds"),
                max_ttl_seconds=command.payload.get("max_ttl_seconds"),
                protocol_version=command.payload.get("protocol_version"),
            )
        except ProviderRegistryError:
            return CommandOutcome.failed("invalid_metadata")
        return CommandOutcome.succeeded(resource_id=provider_id, result={"provider_id": provider_id})

    def _command_provider_set_default(self, command: Command) -> CommandOutcome:
        """`provider_set_default` (§7.12): make one provider the default.
        `set_default()` is transactional (clears the old default atomically,
        ADR-0008); a later registration never silently replaces the default."""
        provider_id = command.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return CommandOutcome.failed("invalid_provider_id")
        try:
            profile = self._provider_registry.set_default(provider_id)
        except ProviderRegistryError:
            return CommandOutcome.failed("provider_not_found")
        return CommandOutcome.succeeded(resource_id=provider_id, result={"provider_id": provider_id})

    def _command_provider_remove(self, command: Command) -> CommandOutcome:
        """`provider_remove` (§7.12): delete the profile outright when
        nothing references it, else disable it (history preserved, ADR-0008).
        Result `{"action": "deleted"|"disabled"}` per §7.9."""
        provider_id = command.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return CommandOutcome.failed("invalid_provider_id")
        if self._provider_registry.resolve(provider_id) is None:
            return CommandOutcome.failed("provider_not_found")
        try:
            action = self._provider_registry.remove_or_disable(
                provider_id, self._workspace_manager, self._principal.principal_id
            )
        except ProviderRegistryError:
            return CommandOutcome.failed("provider_not_found")
        return CommandOutcome.succeeded(resource_id=provider_id, result={"action": action})

    def _command_provider_set_upload_token(self, command: Command) -> CommandOutcome:
        """`provider_set_upload_token` (§7.12): persist an upload token to a
        0600 file, never echoing it. The authoritative length cap lives in
        `set_upload_token()` (worker-side, in the same write); this handler
        surfaces its `upload_token_too_long` code without ever logging the
        token."""
        provider_id = command.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return CommandOutcome.failed("invalid_provider_id")
        if self._provider_registry.resolve(provider_id) is None:
            return CommandOutcome.failed("provider_not_found")
        token = command.payload.get("upload_token")
        if not isinstance(token, str) or not token.strip():
            return CommandOutcome.failed("invalid_metadata")
        try:
            self._provider_registry.set_upload_token(
                provider_id, self._workspace_manager, self._principal.principal_id, token
            )
        except ProviderRegistryError as exc:
            return CommandOutcome.failed(exc.error_code or "invalid_metadata")
        return CommandOutcome.succeeded(
            resource_id=provider_id,
            result={"provider_id": provider_id, "upload_token_configured": True},
        )

    def _command_provider_clear_upload_token(self, command: Command) -> CommandOutcome:
        """`provider_clear_upload_token` (§7.12): remove an upload token.
        Idempotent (no token configured → no-op); result
        `upload_token_configured: false`."""
        provider_id = command.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return CommandOutcome.failed("invalid_provider_id")
        if self._provider_registry.resolve(provider_id) is None:
            return CommandOutcome.failed("provider_not_found")
        try:
            self._provider_registry.clear_upload_token(
                provider_id, self._workspace_manager, self._principal.principal_id
            )
        except ProviderRegistryError:
            return CommandOutcome.failed("provider_not_found")
        return CommandOutcome.succeeded(
            resource_id=provider_id,
            result={"provider_id": provider_id, "upload_token_configured": False},
        )

    def _command_provider_check(self, command: Command) -> CommandOutcome:
        """`provider_check` (§7.12): force a fresh reachability/identity check
        of the provider now (the periodic monitor is lazy). The worker is the
        sole executor of DNS/HTTP here; the fresh state is observable via the
        connectivity snapshot (§3.4), not this command's own result."""
        provider_id = command.payload.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            return CommandOutcome.failed("invalid_provider_id")
        if self._provider_registry.resolve(provider_id) is None:
            return CommandOutcome.failed("provider_not_found")
        self._connectivity.refresh(force=True)
        return CommandOutcome.succeeded(resource_id=provider_id, result={"provider_id": provider_id})

    def _refresh_snapshot(self) -> None:
        """Step 1.6A.1 (correction #1): republish the attachment snapshot
        after this tick's state transitions (§3.3). The worker-owned
        publisher is the only thing that reads `conn` for the request-facing
        read surface, and this runs *after* `_dispatch_outgoing_replies()`
        (and the row-scan before it) so the publish reflects every row this
        tick advanced. Never raises: the publisher's own `refresh()` retains
        the last-known-good snapshot on any build failure, so a failed
        publish cannot kill the worker tick."""
        self._snapshot_publisher.refresh(
            self._conn,
            workspace_id=self._principal.workspace_id,
            workspace_manager=self._workspace_manager,
            principal_id=self._principal.principal_id,
        )

    def _cleanup_committed_reservations(self) -> None:
        """Finding 1 (Correction 3): release a committed create's pending
        reservation only once the published snapshot's idempotency index
        contains the *exact* `(attachment_id, canonical_hash)` pair this
        command committed - not merely a `client_request_id` key (which would
        release early when the id is present but the row's id/hash does not
        match, e.g. a duplicate promote not yet reflected). This closes the
        publish-before-release handoff for both the newly-inserted row and the
        matching-hash IntegrityError recovery path.

        If the snapshot publish failed (snapshot() returns None), every
        handoff is kept and retried on the next tick - a stale/failed publish
        never releases a reservation early."""
        snapshot = self._snapshot_publisher.snapshot()
        if snapshot is None:
            # Publish failed - keep handoffs for retry on next tick
            return
        for client_request_id, handoff in list(self._committed_reservations.items()):
            entry = snapshot.idempotency.get(client_request_id)
            if (
                entry is not None
                and entry.attachment_id == handoff.attachment_id
                and entry.canonical_hash == handoff.canonical_hash
            ):
                del self._committed_reservations[client_request_id]
                if self._pending_reservations is not None:
                    self._pending_reservations.remove(client_request_id)

    def _process_one_inbound_event(self, event: InboundEvent) -> None:
        """Everything that used to run on the radio listener thread
        itself (PR #231 review, section 2) - ingest (base64url decode +
        CBOR parse), message-type dispatch, and, for a KEY_REQUEST/
        KEY_ANNOUNCE/KEY_ACK, the actual KeyExchangeCoordinator call - now
        runs here, on this worker thread, holding this connection.
        Mirrors the dispatch logic that used to live directly in
        mca_runtime.handle_incoming_meshtastic_text()."""
        if self._delivery_adapter is None:
            # No transport configured for this service instance (e.g. a
            # test constructing AttachmentsService without one) - nothing
            # to ingest through. Not an error: a standalone service that
            # only ever processes already-created attachment rows is a
            # legitimate configuration.
            return
        transport_event = {
            "text": event.text, "source_address": event.source_address, "packet_id": event.packet_id,
        }
        envelope = self._delivery_adapter.ingest(transport_event)
        if envelope is None:
            return

        # PR 1 (final correction): canonical node-id validation at the common
        # inbound boundary, BEFORE any message type is decoded or dispatched.
        # For the DIRECT-only MVP the event's `source_address`, the envelope's
        # `source_address`, and the envelope's `route_id` must all be the same
        # canonical `!` + 8-lowercase-hex node id. A non-canonical or
        # internally-mismatched source is dropped outright here, so it can
        # never create a key binding, trigger a key reply, create an
        # attachment, or apply an ACK. (This supersedes the OFFER-only check
        # that used to live in `_process_inbound_offer`, which guarded only
        # one message type and only the bare `source_address`, not the full
        # route identity - a KEY_REQUEST/KEY_ANNOUNCE/KEY_ACK or attachment
        # ACK carrying a bogus source used to slip past it.)
        if (
            not _is_contact_id(event.source_address)
            or not _is_contact_id(envelope.source_address)
            or not _is_contact_id(envelope.route_id)
            or event.source_address != envelope.source_address
            or envelope.source_address != envelope.route_id
        ):
            logger.info(
                "AttachmentsService: rejected inbound MCA message from non-canonical or mismatched source "
                "(event=%r, envelope_source=%r, route_id=%r)",
                event.source_address, envelope.source_address, envelope.route_id,
            )
            return

        try:
            message_type = codec.peek_message_type(envelope.logical_message)
        except codec.CodecError as exc:
            logger.info("AttachmentsService: malformed MCA message from %s: %s", event.source_address, exc)
            return

        if message_type in _INBOUND_ACK_TYPES:
            self._process_inbound_ack(envelope, message_type, source_address=event.source_address)
            return

        if message_type == codec.MessageType.CANCEL:
            self._process_inbound_cancel(envelope, source_address=event.source_address)
            return

        if message_type == codec.MessageType.OFFER:
            self._process_inbound_offer(envelope, source_address=event.source_address)
            return

        try:
            reply_logical = self._key_exchange.handle_incoming(envelope)
        except RateLimited as exc:
            logger.info("AttachmentsService: KEY_REQUEST from %s rate-limited: %s", event.source_address, exc)
            return
        except DeliveryError as exc:
            logger.info("AttachmentsService: malformed MCA message from %s: %s", event.source_address, exc)
            return
        if reply_logical is None:
            return
        self._send_reply_now(reply_logical, event.source_address, idempotency_key=f"mca-reply-{event.packet_id or event.source_address}")

    def _process_inbound_offer(self, envelope: DeliveryEnvelope, *, source_address: str) -> None:
        """PR #231 review, section 5: the OFFER's *own* provider_id
        decides WAITING_CONSENT vs WAITING_NETWORK, never the workspace-
        wide internet flag - a Relay this OFFER doesn't even reference
        being reachable (or not) must never influence this decision.
        `codec.decode_offer(..., verify_key=None)` mirrors exactly what
        `receiver.handle_offer()` itself does internally to reach the
        same field before any signature is checked - this is not a
        second, differently-trusted parse of the same bytes, just reading
        the one field needed one call earlier so the right
        `can_attempt_relay()` argument can be passed in.

        PR #231 review, section 4.2: also builds the `receiver.ReplyRoute`
        `handle_offer()` now persists, straight from the same `envelope`
        `_process_one_inbound_event()` already ingested - `adapter_id`/
        `connector_profile_id`/`route_type`/`route_id` all come from the
        real adapter that actually received this OFFER, not a hardcoded
        Meshtastic assumption. `route_id` is always exactly
        `source_address` for the DIRECT-only MVP (see
        `MeshtasticTextAdapter.ingest()`'s own construction), matching
        `handle_offer()`'s own validation that the two must agree."""
        from meshsrv.attachments.provider_registry import encode_provider_id

        raw_offer = envelope.logical_message
        try:
            unverified = codec.decode_offer(raw_offer, verify_key=None)
            provider_id_text = encode_provider_id(unverified.provider_id)
        except codec.CodecError as exc:
            logger.info("AttachmentsService: rejected OFFER from %s: not well-formed: %s", source_address, exc)
            return
        network_available = self._connectivity.can_attempt_relay(provider_id_text)
        reply_route = receiver.ReplyRoute(
            adapter_id=envelope.adapter_id,
            connector_profile_id=envelope.connector_profile_id,
            route_type=envelope.route_type.value,
            route_id=envelope.route_id,
            destination_address=envelope.route_id,
        )
        try:
            receiver.handle_offer(
                self._conn,
                workspace_manager=self._workspace_manager,
                principal=self._principal,
                provider_registry=self._provider_registry,
                key_exchange=self._key_exchange,
                raw_offer=raw_offer,
                network_available=network_available,
                source_address=source_address,
                reply_route=reply_route,
            )
        except receiver.ReceiverError as exc:
            logger.info("AttachmentsService: rejected OFFER from %s: %s", source_address, exc)

    def _process_inbound_ack(self, envelope: DeliveryEnvelope, message_type, *, source_address: str) -> None:
        """ADR-0009 Decision 7/8, extended by ADR-0010: verify and apply one
        inbound simple ACK against a *sent* attachment (ACK_RECEIVED /
        ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN) or one inbound terminal lifecycle
        ack (REJECTED / EXPIRED) against a *sent* attachment. Every step before
        the final `sender.apply_*` is a read/verify that can only ever drop
        (never write, never reply over the radio). Each drop reason is logged via
        `_drop_ack()` at a sanitized level - a fixed reason token only. No radio
        response is ever sent for any ACK: valid, invalid, unknown, tombstoned,
        duplicate, or stale."""
        raw = envelope.logical_message
        try:
            unverified = codec.decode_simple_ack(raw, message_type, verify_key=None)
        except codec.CodecError:
            self._drop_ack("not_well_formed")
            return

        # 16-byte transfer_id, already validated by the decoder, hex for the
        # lookup. No tombstone/attachment lookup touches anything the decoder
        # did not already bound.
        transfer_id_hex = unverified.transfer_id.hex()
        attachment = self._conn.execute(
            "SELECT * FROM attachments WHERE workspace_id = ? AND transfer_id = ? AND direction = 'sent'",
            (self._principal.workspace_id, transfer_id_hex),
        ).fetchone()
        if attachment is None:
            if is_tombstoned(self._conn, transfer_id_hex):
                # A stale ack racing an orphaned-upload restart (the transfer was
                # tombstoned and re-issued under a fresh id) - expected, not hostile.
                self._drop_ack("tombstoned_transfer", level="info")
            else:
                self._drop_ack("unknown_transfer", level="warning")
            return
        attachment_id = attachment["id"]

        recipients = self._conn.execute(
            "SELECT * FROM attachment_recipients WHERE attachment_id = ?", (attachment_id,)
        ).fetchall()
        deliveries = self._conn.execute(
            "SELECT * FROM attachment_deliveries WHERE attachment_id = ?", (attachment_id,)
        ).fetchall()
        if len(recipients) != 1 or len(deliveries) != 1:
            # Stage 1 scope (Decision 5): a cardinality mismatch is an
            # internal-consistency failure to fail closed on, never guess around.
            self._drop_ack("cardinality", level="warning")
            return
        recipient_row = recipients[0]
        delivery_row = deliveries[0]

        # Decision 3: the ACK must arrive on the exact DIRECT route the OFFER
        # was sent over, as persisted on the delivery row.
        if (
            delivery_row["route_type"] != RouteType.DIRECT.value
            or delivery_row["adapter_id"] != envelope.adapter_id
            or delivery_row["connector_profile_id"] != envelope.connector_profile_id
            or delivery_row["route_id"] != envelope.route_id
        ):
            self._drop_ack("source_route_mismatch", level="warning")
            return

        # Decision 2: verify against the recipient public identity pinned on this
        # transfer. Never `get_binding_by_key_id()` - a valid ACK signed by the
        # old pinned key must still be accepted after a contact-key rotation,
        # while an ACK signed only by the new current key must be rejected.
        pinned = recipient_row["recipient_public_identity"]
        if not isinstance(pinned, bytes) or len(pinned) != 32:
            self._drop_ack("pinned_key_missing", level="warning")
            return
        if compute_key_id(pinned) != recipient_row["recipient_principal_id"]:
            self._drop_ack("pinned_key_mismatch", level="warning")
            return
        try:
            codec.decode_simple_ack(raw, message_type, verify_key=VerifyKey(pinned))
        except codec.CodecError:
            self._drop_ack("signature_invalid", level="warning")
            return

        # ADR-0010: REJECTED/EXPIRED are the sender-side terminal lifecycle
        # acks (Receiver -> Sender). ACK_RECEIVED/ACK_DOWNLOADED/
        # ACK_PROVIDER_UNKNOWN keep the existing `apply_ack` dispatcher.
        if message_type == codec.MessageType.REJECTED:
            sender.apply_rejected(self._conn, attachment_id, now=self._now())
        elif message_type == codec.MessageType.EXPIRED:
            sender.apply_expired(self._conn, attachment_id, now=self._now())
        else:
            sender.apply_ack(self._conn, attachment_id, message_type, now=self._now())

    def _process_inbound_cancel(self, envelope: DeliveryEnvelope, *, source_address: str) -> None:
        """ADR-0010: verify and apply one inbound CANCEL (Sender -> Receiver)
        against a *received* attachment. The CANCEL is signed by the sender,
        and is verified against the sender public identity *pinned on this
        transfer at OFFER admission* (`sender_public_identity`) - never the
        current address binding, so the check is key-rotation-safe (the
        receiver-side mirror of `_process_inbound_ack`'s pinned-recipient
        path). Steps: source-route check against the OFFER's persisted reply
        route, then pinned-key consistency (`compute_key_id(pinned) ==
        sender_principal_id`), then signature verification, then
        `receiver.apply_cancelled()`. Every step before the apply can only
        drop (never write, never reply over the radio) - the same discipline
        as ADR-0009 Decision 7/8."""
        raw = envelope.logical_message
        try:
            unverified = codec.decode_simple_ack(raw, codec.MessageType.CANCEL, verify_key=None)
        except codec.CodecError:
            self._drop_ack("not_well_formed")
            return

        transfer_id_hex = unverified.transfer_id.hex()
        attachment = self._conn.execute(
            "SELECT * FROM attachments WHERE workspace_id = ? AND transfer_id = ? AND direction = 'received'",
            (self._principal.workspace_id, transfer_id_hex),
        ).fetchone()
        if attachment is None:
            if is_tombstoned(self._conn, transfer_id_hex):
                self._drop_ack("tombstoned_transfer", level="info")
            else:
                self._drop_ack("unknown_transfer", level="warning")
            return
        attachment_id = attachment["id"]

        # The CANCEL must arrive on the same DIRECT route the OFFER came in on,
        # as persisted in the OFFER's reply-route columns.
        if (
            attachment["reply_route_type"] != RouteType.DIRECT.value
            or attachment["reply_adapter_id"] != envelope.adapter_id
            or attachment["reply_connector_profile_id"] != envelope.connector_profile_id
            or attachment["reply_route_id"] != envelope.route_id
        ):
            self._drop_ack("source_route_mismatch", level="warning")
            return

        # ADR-0010 Decision 3: verify against the sender public identity
        # pinned on this transfer when the OFFER was admitted - never
        # `get_binding(envelope.route_id)`, which resolves the *current* TOFU
        # binding and would both break a valid CANCEL after a contact-key
        # rotation (the address now resolves to a different key) and let a
        # rotated-out key cancel (only the new key matches the mutable
        # binding). A NULL pinned identity (the OFFER was parked in
        # WAITING_KEY behind an unverified key, or a pre-migration row) is
        # unverifiable and dropped, never guessed at.
        pinned = attachment["sender_public_identity"]
        if not isinstance(pinned, bytes) or len(pinned) != 32:
            self._drop_ack("sender_key_missing", level="warning")
            return
        if compute_key_id(pinned) != attachment["sender_principal_id"]:
            self._drop_ack("sender_key_mismatch", level="warning")
            return
        try:
            codec.decode_simple_ack(raw, codec.MessageType.CANCEL, verify_key=VerifyKey(pinned))
        except codec.CodecError:
            self._drop_ack("signature_invalid", level="warning")
            return

        receiver.apply_cancelled(self._conn, attachment_id, now=self._now())

    def _drop_ack(self, reason: str, *, level: str = "info") -> None:
        """Log one sanitized ACK drop. `reason` is a fixed token only - never the
        raw source address, key bytes/key IDs, signature, transfer/attachment
        IDs, ciphertext, manifest, local path, or exception text (ADR-0009
        Decision 8)."""
        log = logger.info if level == "info" else logger.warning
        log("AttachmentsService: dropped inbound ACK (%s)", reason)

    def _send_reply_now(self, reply_logical: bytes, source_address: str, *, idempotency_key: str) -> None:
        """A KEY_ANNOUNCE/KEY_ACK reply is small, already-signed, and has
        no persisted retry queue of its own (unlike the receiver-side ACK
        outbox below) - sent inline, on this same worker thread, the same
        best-effort/log-and-continue policy the radio listener used to
        apply to it directly."""
        if self._delivery_adapter is None:
            return
        try:
            route = Route(route_type=RouteType.DIRECT, route_id=source_address, destination_address=source_address)
            wire_payload = self._delivery_adapter.encode(reply_logical, route)
            self._delivery_adapter.send(wire_payload, route, idempotency_key=idempotency_key)
        except Exception:  # noqa: BLE001 - must never crash the worker thread
            logger.exception("AttachmentsService: failed to send reply to %s", source_address)

    def _reconcile_receiver_expiry(self) -> None:
        """PR 2.5 (outbound EXPIRED): the bounded receiver-side expiry sweep.
        For every received attachment in a `receiver.EXPIRABLE_STATES` state
        whose authoritative `hard_expires_at` has now passed (positive and
        `<= now`), transition it to EXPIRED and enqueue one signed EXPIRED
        frame to the pinned sender route - atomically, per row, via
        `receiver.expire()`. Bounded to `MAX_RECEIVER_EXPIRY_PER_TICK` rows
        per tick (a large backlog drains over successive ticks); the rest wait
        for the next tick. Restart-safe and duplicate-free by construction:
        `receiver.expire()`'s state guard plus the outbox dedup make a re-run
        a no-op - a row already EXPIRED (or whose frame is already enqueued)
        is skipped, never double-transitioned or double-enqueued. Runs offline
        (it is pure SQLite + enqueue - no network, no radio), so a restart
        resumes the sweep where it left off without a radio round-trip.

        Invalid deadlines are fail-closed here *and* re-checked in
        `receiver.expire()`: the query only selects rows with
        `hard_expires_at IS NOT NULL AND hard_expires_at > 0`, so a NULL/
        zero/negative deadline is never swept, and `expire()` independently
        refuses to transition a row whose deadline has not actually passed."""
        now = self._now()
        placeholders = ",".join("?" for _ in receiver.EXPIRABLE_STATES)
        self._conn.row_factory = sqlite3.Row
        rows = self._conn.execute(
            f"""
            SELECT id FROM attachments
            WHERE workspace_id = ? AND direction = 'received'
              AND state IN ({placeholders})
              AND hard_expires_at IS NOT NULL AND hard_expires_at > 0
              AND hard_expires_at <= ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (
                self._principal.workspace_id,
                *receiver.EXPIRABLE_STATES,
                now,
                MAX_RECEIVER_EXPIRY_PER_TICK,
            ),
        ).fetchall()
        for row in rows:
            try:
                # PR 2.5 correction: each row's expire is its own SAVEPOINT
                # (`_atomic`), so one failing row is rolled back in isolation and
                # later rows still process without ever committing the failed
                # row's partial writes (a state UPDATE/attachment_events/outbox
                # row written before `expire()` raised). `commit=False` folds
                # expire's transition + enqueue into that one savepoint.
                with self._atomic("expire"):
                    receiver.expire(
                        self._conn,
                        workspace_manager=self._workspace_manager,
                        principal=self._principal,
                        attachment_id=row["id"],
                        now=now,
                        commit=False,
                    )
            except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest of the sweep
                # Sanitized: no exception message/traceback (it may embed the
                # row's transfer id or other untrusted material) - only the
                # DB-validated attachment id and the exception class name.
                logger.error(
                    "AttachmentsService: receiver expiry failed for attachment %s: %s",
                    row["id"], type(exc).__name__,
                )

    def _dispatch_outgoing_replies(self) -> None:
        """PR #227 defect #1 (generalized by PR 2.5): the one place a queued
        control message - a receiver-side ACK/REJECTED/EXPIRED, or a
        sender-side CANCEL - (mca_outgoing_replies, receiver.py) is ever
        actually handed to a transport. Runs every tick, after the row-scan
        and expiry sweep above, so a frame enqueued by this same tick is
        dispatched without waiting for a second tick. Rate-limited to
        `receiver.MAX_OUTGOING_REPLY_SENDS_PER_DISPATCH` rows per call
        (receiver.fetch_due_outgoing_replies()'s own LIMIT) - the rest simply
        wait for the next tick, rather than flushing an entire backlog onto
        the radio at once. The route is read from the outbox row's own
        immutable snapshot (migration 17), never re-derived from mutable
        contact data."""
        if self._delivery_adapter is None:
            return
        for reply in receiver.fetch_due_outgoing_replies(
            self._conn, self._principal.workspace_id, self._now()
        ):
            if reply.route_type is None or reply.route_id is None:
                # No route was ever recorded for this reply's attachment
                # (handle_offer() ran without source_address - see
                # PendingReply's own docstring). Nothing to retry
                # towards: terminal, not a backoff case. Fixed sanitized
                # token - never the route values themselves.
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(), error_code=REPLY_ROUTE_MISSING
                )
                continue
            # PR 2.5: the route snapshot is validated strictly before any send
            # - a control message is DIRECT-only, both route_id and
            # destination_address must be canonical contact ids, destination
            # must equal route_id, and the adapter/connector must match this
            # service's own live delivery adapter. Any of these failing means
            # the snapshot is malformed/incomplete (or was written by a route
            # shape this MVP does not support, e.g. a channel) - fail closed to
            # UNDELIVERABLE, never silently redirected, never guessed past.
            if reply.route_type != RouteType.DIRECT.value:
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(),
                    error_code=REPLY_ROUTE_NOT_DIRECT,
                )
                continue
            if not _is_contact_id(reply.route_id) or not _is_contact_id(reply.destination_address):
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(),
                    error_code=REPLY_ROUTE_INVALID_CONTACT,
                )
                continue
            if reply.destination_address != reply.route_id:
                # DIRECT-only MVP: the send target must be the route_id itself.
                # A mismatch means the snapshot came from a non-DIRECT shape we
                # do not support (or a migration bug) - never guess a target.
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(),
                    error_code=REPLY_DESTINATION_MISMATCH,
                )
                continue
            # PR #231 review (3rd pass): "do not silently ignore persisted
            # connector_profile_id" and "fail closed when required route
            # identity is absent or mismatched". This dispatch step used
            # to only read route_type/route_id and send through whichever
            # delivery_adapter this service happened to be constructed
            # with - the persisted adapter_id/connector_profile_id
            # (section 4.2, from the real DeliveryEnvelope that received
            # the OFFER) were written but never actually consulted. Both
            # are now required and strictly validated: a reply whose
            # adapter_id or connector_profile_id is missing (NULL - e.g.
            # handle_offer()'s source_address-only call shape, which
            # records route_type/route_id but no full ReplyRoute) or
            # present-but-mismatched against this service's own
            # delivery_adapter is marked UNDELIVERABLE, the same terminal
            # outcome as a wholly-missing route. This is a deliberate
            # tightening from an earlier pass of this same fix, which
            # treated a missing adapter_id as "trust the current adapter"
            # - inconsistent with the fail-closed posture the rest of
            # this review applies everywhere else (TOFU binding,
            # inbound-OFFER admission): not knowing which adapter/
            # connector a reply belongs to is exactly the situation where
            # guessing must not happen. In production, AttachmentsService.
            # _process_inbound_offer() always builds a full ReplyRoute
            # from the real DeliveryEnvelope, so this never affects a
            # genuine inbound OFFER - only a caller that deliberately
            # chose the simpler, route-identity-free handle_offer() shape
            # (this module's own non-integration tests) now correctly
            # gets an undeliverable reply rather than one silently sent
            # through a possibly-wrong adapter.
            # PR #231 review (4th pass): `adapter_id`/`connector_profile_id`
            # are now required, non-optional parts of the `DeliveryAdapter`
            # contract itself (`delivery/base.py`) - reading them via plain
            # attribute access, not `getattr(..., None)`, which used to
            # silently mask a genuinely broken adapter implementation
            # (one that forgot to declare `connector_profile_id`) as "no
            # connector profile configured", the same outcome as a
            # legitimately-missing persisted value. A real contract
            # violation now raises `AttributeError` loudly instead.
            adapter_id = self._delivery_adapter.adapter_id
            connector_profile_id = self._delivery_adapter.connector_profile_id
            if reply.adapter_id is None or reply.adapter_id != adapter_id:
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(),
                    error_code=REPLY_ADAPTER_MISMATCH,
                )
                continue
            if reply.connector_profile_id is None or reply.connector_profile_id != connector_profile_id:
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(),
                    error_code=REPLY_CONNECTOR_MISMATCH,
                )
                continue
            if not receiver.check_and_record_reply_quota(
                self._conn, self._principal.workspace_id, reply.route_id, self._now()
            ):
                # PR #231 review, section 4.3: a real, persisted quota -
                # per-source-address and global - not just "5 rows per
                # dispatch call" (which only limited one SQL query, not
                # the actual send rate: at the 5s tick interval that
                # still allowed up to 60 sends/minute with zero per-
                # source protection). Left PENDING, retried on a later
                # dispatch once the relevant window has room again -
                # never marked sent, never dropped.
                continue
            route = Route(
                route_type=RouteType(reply.route_type), route_id=reply.route_id,
                destination_address=reply.destination_address,
            )
            try:
                wire_payload = self._delivery_adapter.encode(reply.message, route)
                receipt = self._delivery_adapter.send(wire_payload, route, idempotency_key=f"mca-reply-{reply.id}")
            except Exception as exc:  # noqa: BLE001 - one bad reply must not stop the others or the tick
                # Sanitized: the exception text may embed adapter/transport
                # data, so log only the safe outbox id + exception class, and
                # persist only the fixed token - never str(exc).
                logger.error(
                    "AttachmentsService: failed to send queued reply %s: %s",
                    reply.id, type(exc).__name__,
                )
                receiver.mark_reply_attempt_failed(self._conn, reply.id, self._now(), error_code=DELIVERY_ERROR)
                continue
            if not receipt.sent:
                # PR #231 review, section 4.1: adapter.send() returning
                # normally (no exception) does NOT mean the message was
                # actually sent - DeliveryReceipt.sent is the real
                # signal. Treated exactly like a raised exception: stays
                # PENDING, attempt counted, backoff scheduled.
                logger.warning("AttachmentsService: reply %s not sent (receipt.sent=False)", reply.id)
                receiver.mark_reply_attempt_failed(
                    self._conn, reply.id, self._now(), error_code=DELIVERY_RECEIPT_NOT_SENT
                )
                continue
            receiver.mark_reply_sent(self._conn, reply.id, self._now())

    def _due_rows(self) -> List[sqlite3.Row]:
        self._conn.row_factory = sqlite3.Row
        sent_placeholders = ",".join("?" for _ in sender.AUTOMATIC_STATES)
        received_placeholders = ",".join("?" for _ in receiver.AUTOMATIC_STATES)
        return self._conn.execute(
            f"""
            SELECT id, direction, provider_id FROM attachments
            WHERE workspace_id = ? AND (
                (direction = 'sent' AND state IN ({sent_placeholders}))
                OR (direction = 'received' AND state IN ({received_placeholders}))
            )
            ORDER BY created_at
            LIMIT ?
            """,
            (
                self._principal.workspace_id,
                *sender.AUTOMATIC_STATES,
                *receiver.AUTOMATIC_STATES,
                self._max_per_tick,
            ),
        ).fetchall()

    # ---- per-direction step ------------------------------------------------

    def _step_sent(self, row: sqlite3.Row) -> None:
        attachment_id = row["id"]
        state = sender.get_state(self._conn, attachment_id)
        recipient_identities = None
        if state == sender.ENCRYPTING:
            recipient_identities = self._resolve_recipient_identities(attachment_id)
            required_key_ids = self._required_recipient_key_ids(attachment_id)
            if any(key_id not in recipient_identities for key_id in required_key_ids):
                # Reviewer-found defect (PR #227 defect #6): at least one
                # recipient's key_exchange binding is not currently
                # trusted (see _resolve_recipient_identities()'s
                # docstring) - fail the whole attachment now, before any
                # envelope is sealed to an unverified or superseded key,
                # rather than handing sender.run_step() a partial mapping
                # and letting _public_identity_for() raise mid-encrypt.
                sender.fail_recipients_not_trusted(self._conn, attachment_id, self._now())
                return
        provider_id_text = _provider_id_text(row["direction"], row["provider_id"])
        relay_client = self._relay_client_factory(provider_id_text) if provider_id_text else None
        sender.run_step(
            self._conn,
            workspace_manager=self._workspace_manager,
            principal=self._principal,
            recipient_identities=recipient_identities,
            relay_client=relay_client,
            delivery_adapter=self._delivery_adapter,
            network_available=self._can_attempt(provider_id_text),
            attachment_id=attachment_id,
            now=self._now(),
        )

    def _step_received(self, row: sqlite3.Row) -> None:
        provider_id_text = _provider_id_text(row["direction"], row["provider_id"])
        relay_client = self._relay_client_factory(provider_id_text) if provider_id_text else None
        receiver.run_step(
            self._conn,
            workspace_manager=self._workspace_manager,
            principal=self._principal,
            provider_registry=self._provider_registry,
            key_exchange=self._key_exchange,
            network_available=self._can_attempt(provider_id_text),
            attachment_id=row["id"],
            relay_client=relay_client,
            now=self._now(),
        )

    def _can_attempt(self, provider_id_text: Optional[str]) -> bool:
        """Feeds `ConnectivityMonitor.can_attempt_relay()` (advisory,
        fail-open on an unknown provider_id) into the exact
        `network_available: bool` parameter both state machines already
        take - their signatures are unchanged by ADR-0008 (decision 2)."""
        if provider_id_text is None:
            return False
        return self._connectivity.can_attempt_relay(provider_id_text)

    def _resolve_recipient_identities(self, attachment_id: str) -> Dict[str, bytes]:
        """`run_step()`'s ENCRYPTING handler needs `{key_id_hex:
        public_identity_bytes}` for every recipient. `attachment_recipients.
        recipient_principal_id` holds each recipient's key_id (create_draft()'s
        own INSERT) - re-resolving the public identity from `key_exchange`'s
        bindings table here is exactly what a caller driving run_step() after a
        restart, rather than right after create_draft(), has to do instead of
        reusing an in-memory value that no longer exists.

        ADR-0009 note: `attachment_recipients` *does* now persist the identity
        in `recipient_public_identity` (Migration 14), but that is the pinned
        copy for inbound-ACK verification, not the value ENCRYPTING seals with.
        This method still re-resolves from the bindings table so the seal uses
        the *currently-trusted* key and fails closed on rotation (below), while
        the pinned copy keeps trusting the original key for ACK verification.

        Reviewer-found defect (PR #227 defect #6): this used to hand back
        `binding.public_identity` for ANY binding it found, trusted or
        not - so an attachment could be encrypted and sent to a recipient
        whose key was never TOFU-confirmed, or whose key_exchange binding
        had since received a conflicting KEY_ANNOUNCE (`KEY_CHANGED`,
        parked in `pending_public_identity` until the user explicitly
        accepts it). A recipient is only included here when their binding
        is currently `AddressStatus.MCA_READY` - `_step_sent()` treats
        any recipient missing from this mapping as a reason to fail the
        whole attachment (`sender.fail_recipients_not_trusted()`) rather
        than seal a copy to an unverified or superseded key.

        PR #231 review, section 8 (independently re-verified, not just
        trusted from the earlier fix above): `key_exchange.
        get_binding_by_key_id()` is deliberately address-agnostic (its
        own docstring: "regardless of whether this particular delivery
        arrived over the same transport address the binding was
        originally established on") - it exists for OFFER signature
        verification, where that is correct (the signer's identity, not
        the physical address an OFFER arrived over, is what matters
        there). Reusing it here, unchanged, for the *sending* path would
        have been a real gap: TOFU's entire guarantee is "this public key
        belongs to whoever answers at this specific address" - encrypting
        for a trusted key_id without confirming this attachment's own
        DIRECT destination address is the *same* address that key was
        actually TOFU-pinned at would silently decouple "who we trust"
        from "where we're sending", exactly the property TOFU exists to
        bind together.

        PR #231 review (3rd pass), tightened further: the earlier fix
        above only checked the transport address when a delivery record
        happened to exist and be DIRECT - a *missing* delivery record or
        a non-DIRECT one silently skipped the address check entirely,
        letting every otherwise-trusted recipient through with no TOFU-
        to-destination binding verified at all. That was backwards: TOFU
        verification before encryption must fail closed on exactly those
        cases, not fail open. Now, before resolving any individual
        recipient: (1) an attachment must have a delivery record at all
        (`attachment_deliveries` - `create_draft()`'s own required
        `adapter_id`/`connector_profile_id`/`route_type`/`route_id`
        parameters mean a normal attachment always has exactly one; a
        missing row is a data-integrity problem, not a shape this MVP's
        own API can legitimately produce); (2) for the current MVP, that
        delivery's `route_type` must be `DIRECT` (the only route shape
        this MVP ever creates or knows how to verify an address against
        - see `ReplyRoute`'s own docstring on the same DIRECT-only
        reasoning for the receiver side); (3) the delivery's own
        `adapter_id` must match this service's actually-configured
        `delivery_adapter.adapter_id` - encrypting for a delivery
        record that names a *different* adapter than the one this
        service is about to send through would be exactly the same
        "trust the key, but not the destination" gap the transport-
        address check already closes, just one field over. Any of these
        three failing excludes every recipient (empty mapping) - the
        same fail-closed outcome `_step_sent()` already treats as
        `fail_recipients_not_trusted()` for the whole attachment.

        Once a delivery record passes all three, each individual
        recipient's binding must also match on two independent fields
        (PR #231 review, 4th pass adds the first of these - the earlier
        pass only checked the second): `binding.adapter_id` must equal
        the delivery's own `adapter_id` (a binding recorded for the
        right address but under a *different* adapter is not the same
        trust relationship as one recorded for the adapter this
        attachment is actually being sent through), and
        `binding.transport_address` must equal the delivery's own
        `route_id`. Fail closed (excluded, same as an untrusted/
        KEY_UNKNOWN binding) on either mismatch, never silently sent
        anyway.

        Logging note (PR #231 review, 3rd pass): none of the log lines
        below include a raw transport address, key_id, or attachment_id
        - only which *kind* of check failed, so a rejection is
        diagnosable from logs without them becoming a secondary channel
        for exactly the identifying data TOFU/binding checks exist to
        protect."""
        self._conn.row_factory = sqlite3.Row
        recipient_rows = self._conn.execute(
            "SELECT DISTINCT recipient_principal_id FROM attachment_recipients WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchall()
        delivery_row = self._conn.execute(
            "SELECT adapter_id, route_type, route_id FROM attachment_deliveries WHERE attachment_id = ? ORDER BY id LIMIT 1",
            (attachment_id,),
        ).fetchone()
        identities: Dict[str, bytes] = {}

        if delivery_row is None:
            logger.info("AttachmentsService: excluding all recipients - no delivery record found for this attachment")
            return identities
        if delivery_row["route_type"] != RouteType.DIRECT.value:
            logger.info(
                "AttachmentsService: excluding all recipients - unsupported (non-DIRECT) delivery route for this MVP"
            )
            return identities
        configured_adapter_id = self._delivery_adapter.adapter_id if self._delivery_adapter is not None else None
        if delivery_row["adapter_id"] != configured_adapter_id:
            logger.info(
                "AttachmentsService: excluding all recipients - delivery record's adapter does not match "
                "the currently configured delivery adapter"
            )
            return identities

        delivery_route_id = delivery_row["route_id"]
        delivery_adapter_id = delivery_row["adapter_id"]
        for recipient_row in recipient_rows:
            key_id = recipient_row["recipient_principal_id"]
            if not key_id:
                continue
            binding = self._key_exchange.get_binding_by_key_id(key_id)
            if binding is None or binding.status != AddressStatus.MCA_READY:
                continue
            # PR #231 review (4th pass): the binding's own adapter_id must
            # also match this delivery's adapter_id - a binding recorded
            # for the right address but under a *different* adapter (e.g.
            # a hypothetical future second adapter reusing an overlapping
            # address namespace) is not the same trust relationship as one
            # recorded for the adapter this attachment is actually being
            # sent through. Checked in addition to, not instead of, the
            # transport_address check below - both must agree.
            if binding.adapter_id != delivery_adapter_id:
                logger.info(
                    "AttachmentsService: excluding a recipient - TOFU binding was recorded under a "
                    "different adapter than this attachment's own delivery adapter"
                )
                continue
            if binding.transport_address != delivery_route_id:
                logger.info(
                    "AttachmentsService: excluding a recipient - TOFU binding is pinned to a different "
                    "transport address than this attachment's own delivery destination"
                )
                continue
            identities[key_id] = binding.public_identity
        return identities

    def _required_recipient_key_ids(self, attachment_id: str) -> List[str]:
        """The full recipient list `_resolve_recipient_identities()` above
        is filtering - kept as its own query (rather than folded into
        that method's return value) so `_step_sent()` can tell "no
        recipients at all" (nothing to compare against - unreachable in
        practice, create_draft() already refuses that) apart from "some
        recipients resolved, some didn't" without the caller needing to
        re-derive the untrusted set from a dict of only the trusted
        ones."""
        self._conn.row_factory = sqlite3.Row
        recipient_rows = self._conn.execute(
            "SELECT DISTINCT recipient_principal_id FROM attachment_recipients WHERE attachment_id = ?",
            (attachment_id,),
        ).fetchall()
        return [row["recipient_principal_id"] for row in recipient_rows if row["recipient_principal_id"]]

    def _default_relay_client(self, provider_id_text: str) -> Optional[RelayClient]:
        """The production factory: resolve the pinned profile and its
        upload token (None is fine - RelayClient's download-path calls
        never need a bearer token, only the upload ones do) from the
        already-injected `ProviderRegistry`. Tests inject their own
        `relay_client_factory` instead of exercising real HTTPS/file I/O
        through this path."""
        profile = self._provider_registry.resolve(provider_id_text)
        if profile is None:
            return None
        upload_token = self._provider_registry.get_upload_token(
            provider_id_text, self._workspace_manager, self._principal.principal_id
        )
        return RelayClient(profile.origin, upload_access_token=upload_token)
