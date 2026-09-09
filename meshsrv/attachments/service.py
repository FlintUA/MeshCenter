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

import dataclasses
import logging
import os
import queue
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

from meshsrv.attachments import codec, crypto, receiver, sender
from meshsrv.attachments.command_registry import CommandRegistry
from meshsrv.attachments.commands import MAX_COMMANDS_PER_TICK, Command, CommandQueue
from meshsrv.attachments.delivery.base import DeliveryAdapter, DeliveryEnvelope, DeliveryError, Route, RouteType
from meshsrv.attachments.dispatch import COMMAND_EXECUTION_FAILED, CommandDispatcher, CommandOutcome
from meshsrv.attachments.identity import MCAPrincipal
from meshsrv.attachments.idempotency import PendingReservation, PendingReservations, validate_canonical_hash, validate_client_request_id
from meshsrv.attachments.key_exchange import AddressStatus, KeyExchangeCoordinator, RateLimited
from meshsrv.attachments.provider_registry import ProviderRegistry, ProviderRegistryError, decode_provider_id, encode_provider_id
from meshsrv.attachments.recipient_snapshot import RecipientSnapshotPublisher
from meshsrv.attachments.relay_client import RelayClient, RelayError, RelayHTTPError
from meshsrv.attachments.snapshots import AttachmentsSnapshotPublisher
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
        placeholder this service would otherwise default to). The five command
        handlers wired so far - the idempotent create (`attachment_create`,
        Step 1.6A.3B), the three Step 1.6A.3A lifecycle commands
        (retry/download/reject), and the Step 1.6A.3C cancel mutation - every
        other enumerated kind stays `unsupported_command_kind` until its own
        future sub-stage wires it.
        Built once at construction (the dispatcher snapshots its mapping),
        never mutated afterwards."""
        return CommandDispatcher({
            "attachment_create": self._command_create,
            "attachment_retry": self._command_retry,
            "attachment_download": self._command_download,
            "attachment_reject": self._command_reject,
            "attachment_cancel": self._command_cancel,
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
        `WAITING_CONSENT` row to `REJECTED` (§7.3). Delegates to
        `receiver.reject()` unchanged, which only transitions the local row to
        `REJECTED` and commits - it does **not** enqueue or send a signed
        `MessageType.REJECTED` frame back to the sender. A real wire-level
        rejection (signed REJECTED generation, durable outbox delivery,
        sender-side source/key verification, and sender-state handling) is
        deliberately deferred to the still-pending inbound control-message /
        ADR-0009 work, so a rejection round trip to the sender is *not*
        complete after this command."""
        attachment_id = command.payload.get("attachment_id")
        row = self._attachment_row(attachment_id)
        if row is None:
            return CommandOutcome.failed("attachment_not_found")
        if row["direction"] != "received" or row["state"] != receiver.WAITING_CONSENT:
            return CommandOutcome.failed("invalid_state_transition")
        try:
            receiver.reject(self._conn, attachment_id, now=self._now())
        except receiver.ReceiverError:
            return CommandOutcome.failed("invalid_state_transition")
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

        try:
            message_type = codec.peek_message_type(envelope.logical_message)
        except codec.CodecError as exc:
            logger.info("AttachmentsService: malformed MCA message from %s: %s", event.source_address, exc)
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

    def _dispatch_outgoing_replies(self) -> None:
        """PR #227 defect #1: the one place a queued receiver-side ACK
        (mca_outgoing_replies, receiver.py) is ever actually handed to a
        transport. Runs every tick, after the row-scan above, so a reply
        enqueued by this same tick's own _step_received() calls is
        dispatched without waiting for a second tick. Rate-limited to
        `receiver.MAX_OUTGOING_REPLY_SENDS_PER_DISPATCH` rows per call
        (receiver.fetch_due_outgoing_replies()'s own LIMIT) - the rest
        simply wait for the next tick, rather than flushing an entire
        backlog onto the radio at once."""
        if self._delivery_adapter is None:
            return
        for reply in receiver.fetch_due_outgoing_replies(
            self._conn, self._principal.workspace_id, self._now()
        ):
            if reply.route_type is None or reply.route_id is None:
                # No route was ever recorded for this reply's attachment
                # (handle_offer() ran without source_address - see
                # PendingReply's own docstring). Nothing to retry
                # towards: terminal, not a backoff case.
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(), error_code="no_reply_route_recorded"
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
                    error_code=f"reply_adapter_mismatch:persisted={reply.adapter_id!r},configured={adapter_id!r}",
                )
                continue
            if reply.connector_profile_id is None or reply.connector_profile_id != connector_profile_id:
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(),
                    error_code=(
                        f"reply_connector_mismatch:persisted={reply.connector_profile_id!r},"
                        f"configured={connector_profile_id!r}"
                    ),
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
            # PR #231 review (4th pass): a missing destination_address is
            # now fail-closed (UNDELIVERABLE), not silently defaulted to
            # route_id. destination_address (not bare route_id) is the
            # actual send target - kept as a separate persisted field
            # precisely so a future non-DIRECT route shape (where "reply
            # to" is not simply "the same address route_id already
            # names") does not have to change this call site, only stop
            # conflating them (ReplyRoute's own docstring, receiver.py).
            # For the DIRECT-only MVP the two are structurally always
            # equal by the time this line is reached (ReplyRoute.
            # destination_address is a required, non-Optional dataclass
            # field, and the adapter_id/connector_profile_id checks above
            # already reject any row that was persisted without a full
            # ReplyRoute) - this check has no known way to trigger today,
            # but is made explicit anyway rather than left as an implicit
            # "or route_id" fallback with no stated route-type contract
            # justifying it, since a silent fallback is exactly the kind
            # of masked assumption a future route shape (or a data
            # migration bug) could quietly violate.
            if reply.destination_address is None:
                receiver.mark_reply_undeliverable(
                    self._conn, reply.id, self._now(), error_code="reply_destination_address_missing"
                )
                continue
            route = Route(
                route_type=RouteType(reply.route_type), route_id=reply.route_id,
                destination_address=reply.destination_address,
            )
            try:
                wire_payload = self._delivery_adapter.encode(reply.message, route)
                receipt = self._delivery_adapter.send(wire_payload, route, idempotency_key=f"mca-reply-{reply.id}")
            except Exception as exc:  # noqa: BLE001 - one bad reply must not stop the others or the tick
                logger.exception("AttachmentsService: failed to send queued reply %s", reply.id)
                receiver.mark_reply_attempt_failed(self._conn, reply.id, self._now(), error_code=str(exc))
                continue
            if not receipt.sent:
                # PR #231 review, section 4.1: adapter.send() returning
                # normally (no exception) does NOT mean the message was
                # actually sent - DeliveryReceipt.sent is the real
                # signal. Treated exactly like a raised exception: stays
                # PENDING, attempt counted, backoff scheduled.
                logger.warning("AttachmentsService: reply %s not sent (receipt.sent=False)", reply.id)
                receiver.mark_reply_attempt_failed(
                    self._conn, reply.id, self._now(), error_code="delivery_receipt_sent_false"
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
        public_identity_bytes}` for every recipient (sender.py's own
        docstring: it deliberately doesn't persist that value a second
        time). `attachment_recipients.recipient_principal_id` holds each
        recipient's key_id (create_draft()'s own INSERT) - re-resolving
        the public identity from `key_exchange`'s bindings table here is
        exactly what a caller driving run_step() after a restart, rather
        than right after create_draft(), has to do instead of reusing an
        in-memory value that no longer exists.

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
