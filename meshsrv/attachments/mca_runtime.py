"""meshsrv/attachments/mca_runtime.py

Execution Plan Step 1.3's "Core's own wiring layer" (see
key_exchange.py's own module docstring: "Core's own wiring layer, not
this module, is the only place that needs to know AdapterIPCTransport
exists at all"). This is that layer's single entry point -
`handle_incoming_meshtastic_text()` - the one function server.py's
radio listener calls after saving a normal incoming direct message.
MIT Core code; imports only meshsrv.* and meshsrv.attachments.*, never
`meshtastic` or anything under `adapters/meshtastic/`.

Owns the process-lifetime singletons this integration needs:
  - the one sqlite3 connection backing this instance's MCA state
    (mca_principal, mca_recipient_bindings, mca_key_exchange_* -
    meshsrv.attachments.db.migrations' schema);
  - the one MCAWorkspaceManager (instance-scoped - identity.py's own
    docstring: an MCA principal is not tied to a radio profile and must
    survive a radio-profile swap, so this is constructed from the same
    top-level data directory server.py's own instance-scoped files use,
    never from PROFILE_DATA_DIR - see MCAWorkspaceManager itself for
    exactly which directory it resolves everything under; this module
    never computes that path itself, only passes through what it's
    given, per workspace.py's own "no other module may build one of
    these paths" rule);
  - the one MCAPrincipal + KeyExchangeCoordinator for the "meshtastic"
    adapter_id (design spec 7.1: exactly one MCA principal per
    workspace, and this MVP has exactly one workspace, "local", for
    the life of the instance).

ADR-0008 extends this module's singleton with the four backend pieces
that ADR needed (`ProviderRegistry`, `ConnectivityMonitor`,
`AttachmentsService`). `ProviderRegistry`/`ConnectivityMonitor` are
constructed eagerly in `_MCARuntimeState.__init__` (cheap - no thread,
no network I/O until `refresh()`/`service.start()` are explicitly
called); `AttachmentsService` itself is *not* constructed there,
because it needs `radio_transport` (only ever handed to us per-call by
`handle_incoming_meshtastic_text()`'s caller, never stored) to build
the one `MeshtasticTextAdapter` it sends through - see
`start_attachments_service()`'s own docstring.

PR #231 review, section 2 (single-owner SQLite model) - and its 2nd-pass
correction, below: this module's own `_lock`/`state.conn` are no longer
touched by `handle_incoming_meshtastic_text()` at all. That function now
does exactly one thing - build an immutable `service.InboundEvent` from
the raw text/metadata and hand it to `AttachmentsService.enqueue_inbound()`
(a bounded, non-blocking queue) - and returns. Every real MCA
message-type dispatch (KEY_REQUEST/KEY_ANNOUNCE/KEY_ACK via
`KeyExchangeCoordinator`, an OFFER via `receiver.handle_offer()`), the
CBOR decode/signature verification that dispatch needs, and every
database write now happen only on `AttachmentsService`'s own worker
thread, draining that same queue at the start of each tick (see
`service.py`'s `_drain_inbound_events()`/`_process_one_inbound_event()`).
The radio listener thread (`server.py`'s `process_message_line()`,
via this function) therefore never blocks on SQLite, Relay network
I/O, or attachment cryptography - it cannot, since it no longer reaches
any of them.

CORRECTION (2nd-pass review): the first version of this redesign still
had an indirect blocking path the module docstring above did not
account for. `handle_incoming_meshtastic_text()` called `_get_state()`
and `state.ensure_service()` on *every* invocation (not just the first),
and both acquired this module's own `_lock` - the *same* lock object
`AttachmentsService` was constructed with (`lock=_lock` in
`_ensure_service_locked()`, since removed) for `tick()`'s own
re-entrancy guard. Since `tick()` holds that lock for its *entire*
duration - including `ConnectivityMonitor.refresh()`'s real Relay HTTP
calls - a slow/unreachable Relay meant the radio listener thread would
block on `_lock` for the same duration, exactly the hazard this whole
redesign exists to eliminate. Confirmed via direct code reading, not
assumed, and reproduced by test (see `tests/test_mca_runtime.py`'s
`test_handle_incoming_meshtastic_text_does_not_block_on_a_slow_tick`).

Fixed with two changes: (1) `AttachmentsService` is now given its own,
dedicated `_tick_lock` (constructed fresh in `_MCARuntimeState.__init__`,
never `mca_runtime._lock`) - the singleton-bookkeeping lock and the
tick-reentrancy lock are two genuinely independent concerns now, not
accidentally sharing one object; (2) `handle_incoming_meshtastic_text()`
no longer calls `ensure_service()`/`_get_state()` at all after startup -
it only ever reads the already-initialized module-level `_state`
directly (a single reference read, safe without a lock under the GIL)
and calls straight through to `_state.service.enqueue_inbound()`, which
itself touches only the thread-safe `queue.Queue` and a `threading.Event`
- never `_lock`, never SQLite, never the filesystem, never the network.
`start_attachments_service()` remains the *only* place that performs
real initialization (`_get_state()`/`ensure_service()`), and `server.py`
now calls it before starting the radio listener (`start_runtime()`), not
after - see that call site's own comment. If the runtime is somehow not
ready yet when a message arrives (should never happen in production
given that ordering, but a test or a future call site could still reach
this function without it), `handle_incoming_meshtastic_text()` fails
fast: it logs and drops the event, exactly like a full inbound queue -
it never falls back to initializing the database or the worker from the
listener thread itself.

DEVIATION FROM ADR-0003, flagged explicitly rather than silently: that
ADR's own wording names a schema path nested one level deeper than
what this module actually uses (under the per-principal workspace
directory). That literal nesting cannot be resolved before the very
first principal exists - `identity.ensure_principal()` takes an
*already-open, already-migrated* connection and only then generates
the fresh key whose id would name that directory, a real chicken-and-
egg gap in the already-merged Step 1.2 API surface, not something
introduced here. Since spec 7.1 fixes "one principal per workspace"
and this MVP never has more than one workspace, per-principal and
per-instance are the same directory in practice today - this module
resolves the gap with one fixed, workspace-independent database file
(directly under `MCAWorkspaceManager.mca_dir`, the same already-
sanctioned root every per-principal workspace nests under) instead of
the per-principal nesting the ADR describes. Revisit only if/when
multiple MCA workspaces in one installation ever become real (out of
scope for Stage 1).
"""

from __future__ import annotations

import logging
import queue
import sqlite3
import threading
import time
from typing import Optional

from meshsrv.attachments import receiver, sender
from meshsrv.attachments.command_registry import CommandRegistry
from meshsrv.attachments.commands import CommandQueue
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
from meshsrv.attachments.dispatch import CommandDispatcher
from meshsrv.attachments.facade import AttachmentsFacade
from meshsrv.attachments.identity import MCAPrincipal, ensure_principal
from meshsrv.attachments.idempotency import PendingReservations
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.probe_registry import ProbeRegistry
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.recipient_snapshot import RecipientSnapshotPublisher
from meshsrv.attachments.service import INBOUND_QUEUE_MAXSIZE, AttachmentsService, InboundEvent
from meshsrv.attachments.snapshots import AttachmentsSnapshotPublisher
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor
from meshsrv.radio_transport import RadioTransport

logger = logging.getLogger(__name__)

WORKSPACE_ID = "local"
ADAPTER_ID = "meshtastic"

# Guards only this module's own singleton bookkeeping (_state creation in
# _get_state(), reset in reset_state_for_tests()) - genuinely nothing
# else. PR #231 review (2nd pass): this lock must never be handed to
# AttachmentsService as its tick lock (see _MCARuntimeState.__init__'s
# own _tick_lock, a separate, freshly-constructed Lock) - the two are
# unrelated concerns (rare, startup-only singleton creation vs. a lock
# held for a tick()'s entire duration, including real Relay HTTP calls),
# and sharing one object between them was the root cause of a real
# listener-thread-blocks-on-a-slow-tick bug the first version of this
# redesign introduced (see the module docstring's own "CORRECTION"
# section). handle_incoming_meshtastic_text() - the radio listener's own
# call path - never acquires this lock at all after startup; only
# _get_state()/ensure_service()/reset_state_for_tests() do, and those are
# only ever called from start_runtime()'s own startup sequence (before
# the listener thread exists) or from tests.
_lock = threading.Lock()
_state: "Optional[_MCARuntimeState]" = None

# Test-only hook (never set in production): lets a test give the
# ConnectivityMonitor this module constructs a fake `requests`-shaped
# session before any tick can run, instead of the real
# `requests.Session()` ConnectivityMonitor defaults to. Needed because
# of a real behavior change from the section-3 "first tick runs
# immediately" fix - before that fix, a fast-running test's own
# assertions and reset_state_for_tests() call typically completed well
# within the old 5-second wait before the very first tick would ever
# fire, so this never surfaced; now the first tick (and therefore
# ConnectivityMonitor.refresh()'s real network call on a workspace with
# no registered providers yet) runs immediately on ensure_service(),
# which a test with no network access could otherwise block on for the
# monitor's own DEFAULT_TIMEOUT_SECONDS.
_test_connectivity_session_override: "Optional[object]" = None


def set_connectivity_session_for_tests(session: "Optional[object]") -> None:
    """Test-only. Must be called before the first `_get_state()`/
    `handle_incoming_meshtastic_text()`/`start_attachments_service()`
    call for a given `data_dir` - `_MCARuntimeState.__init__` reads this
    exactly once, at construction. Not called anywhere in production
    code."""
    global _test_connectivity_session_override
    _test_connectivity_session_override = session


class _MCARuntimeState:
    def __init__(self, data_dir: str):
        # MCAWorkspaceManager(data_dir) resolves and creates its own root
        # internally (see workspace.py's __init__) - this module passes
        # the plain instance data directory straight through and never
        # appends anything itself, per workspace.py's "no other module
        # may build one of these paths" rule (test_no_stray_data_mca_
        # path_construction enforces this by grep, comments included).
        self.workspace_manager = MCAWorkspaceManager(data_dir)
        db_path = self.workspace_manager.mca_dir / "attachments.db"
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        migrate(self.conn)
        self.principal: MCAPrincipal = ensure_principal(self.conn, self.workspace_manager, WORKSPACE_ID)
        self.coordinator = KeyExchangeCoordinator(
            self.conn, self.workspace_manager, self.principal, ADAPTER_ID
        )
        # Finding 7 (Step 1.6A.3B review): the worker-published immutable
        # recipient-binding snapshot. Constructed eagerly (its own __init__
        # does an eager `refresh()`, a SQLite read via `coordinator.
        # list_bindings()` on this startup thread - safe, same reasoning as
        # ConnectivityMonitor's eager `_refresh_profile_snapshot()` below) and
        # handed to *both* the facade (request thread reads it) and the
        # service (worker refreshes it each tick), so they share one instance.
        self.recipient_snapshot_publisher = RecipientSnapshotPublisher(
            self.coordinator, adapter_id=ADAPTER_ID
        )
        # ADR-0008 decision 2/3: both are cheap to construct (plain SQL
        # wrappers - no thread, no network I/O happens until refresh()
        # is explicitly called) so they're built eagerly here rather than
        # deferred to ensure_service() below. This means an inbound OFFER
        # can be handled correctly (see handle_incoming_meshtastic_text())
        # even in the narrow startup window before server.py's
        # start_attachments_service() call has actually run.
        self.provider_registry = ProviderRegistry(self.conn, WORKSPACE_ID)
        self.connectivity_monitor = ConnectivityMonitor(
            self.provider_registry, session=_test_connectivity_session_override
        )
        # PR #231 review, section 2: constructed here, eagerly, rather
        # than deferred to ensure_service() below - a message can arrive
        # (and needs somewhere safe to queue) in the narrow window before
        # AttachmentsService itself exists. The *same* queue object is
        # handed to AttachmentsService once it is constructed, so nothing
        # queued during that window is a second, throwaway queue that
        # gets silently dropped.
        self.inbound_queue: "queue.Queue[InboundEvent]" = queue.Queue(maxsize=INBOUND_QUEUE_MAXSIZE)
        # PR #231 review (2nd pass), requirement 4: AttachmentsService's
        # own tick-reentrancy lock, constructed fresh here and never
        # shared with this module's `_lock` (singleton bookkeeping only -
        # see that name's own comment). Sharing one lock object between
        # "guards _state creation, held briefly, startup-only" and "held
        # for a tick()'s entire duration, including real Relay HTTP calls"
        # was the root cause of a real bug: handle_incoming_meshtastic_
        # text() used to call ensure_service() unconditionally, which
        # acquired that same shared lock - blocking the radio listener
        # thread for as long as a slow Relay request kept a tick running.
        # See the module docstring's "CORRECTION" section for the full
        # account.
        self.tick_lock = threading.Lock()

        # Step 1.6A.1 (correction #1): this state owns the six request-
        # /worker-facing components of the facade plumbing, constructed
        # eagerly here (all cheap - in-memory stores and a publisher, no
        # thread, no SQLite write, no network I/O until the worker tick
        # runs) so the facade is available as soon as the state exists,
        # and so the *same* single instances are shared between the
        # request-facing facade and the worker-facing service below.
        #
        # `wake_event` is the one shared wake signal: the facade's
        # `submit()` sets it (waking the worker to drain the command
        # queue), and `AttachmentsService` waits on it - one Event object,
        # not two, so a submit from a request thread wakes the real
        # worker. `dispatcher` starts as a placeholder empty table - a
        # command of any enumerated kind through it is therefore a stable
        # terminal `unsupported_command_kind` FAILED (dispatch.py). The real
        # kind->handler table is built when the service is (see
        # `_ensure_service_locked`): the handlers are the service's own
        # methods, so they can only exist once the service (and its
        # worker-owned collaborators) does. `_dispatcher_placeholder` is the
        # sentinel that distinguishes "still the placeholder" from "a test
        # injected a custom dispatcher" at service-construction time.
        self.wake_event = threading.Event()
        # Step 1.6A.1 (correction #1): the shared runtime-readiness signal,
        # handed to *both* the facade (which gates on it) and the service
        # (which sets/clears it) so a request thread and the worker agree on
        # a single "started and first snapshot published" truth.
        self.ready_event = threading.Event()
        self.command_queue = CommandQueue()
        self.command_registry = CommandRegistry()
        self.pending_reservations = PendingReservations()
        self.probe_registry = ProbeRegistry()
        self.snapshot_publisher = AttachmentsSnapshotPublisher()
        self._dispatcher_placeholder = CommandDispatcher({})
        self.dispatcher = self._dispatcher_placeholder
        self.facade = AttachmentsFacade(
            command_queue=self.command_queue,
            command_registry=self.command_registry,
            pending_reservations=self.pending_reservations,
            probe_registry=self.probe_registry,
            snapshot_publisher=self.snapshot_publisher,
            wake_event=self.wake_event,
            ready_event=self.ready_event,
            connectivity_monitor=self.connectivity_monitor,
            principal=self.principal,
            workspace_manager=self.workspace_manager,
            recipient_snapshot_publisher=self.recipient_snapshot_publisher,
        )
        # Unlike the pieces above, the worker thread itself is not
        # started until ensure_service() runs - see that method's own
        # docstring for why it needs radio_transport, which this
        # constructor never receives.
        self.service: Optional[AttachmentsService] = None

    def ensure_service(self, radio_transport: RadioTransport) -> AttachmentsService:
        """ADR-0008 decision 1's five-step startup sequence, steps 2-4
        (step 1, migrate(), already happened in __init__ above). Building
        `AttachmentsService` needs a `MeshtasticTextAdapter`, which in
        turn needs `radio_transport` - the one piece `_MCARuntimeState`
        cannot construct itself, since today it only ever reaches this
        module per-call, via `start_attachments_service()`'s own
        parameter, never as something stored at process startup.

        PR #231 review (2nd pass): this method - and therefore this
        module's own `_lock` - is now called ONLY from `start_attachments_
        service()`, which `server.py`'s `start_runtime()` calls once,
        synchronously, before the radio listener thread is started at
        all (see that call site's own comment). `handle_incoming_
        meshtastic_text()` no longer calls this method on every message;
        it only ever reads the already-built `_state`/`_state.service`
        directly (module-level docstring). This method stays idempotent
        and self-locking regardless - a second real startup attempt (a
        hypothetical future profile-swap re-init path) must still be
        safe - but the lock it acquires is no longer anywhere near the
        radio listener's own hot path.
        """
        with _lock:
            return self._ensure_service_locked(radio_transport)

    def _ensure_service_locked(self, radio_transport: RadioTransport) -> AttachmentsService:
        if self.service is not None:
            return self.service
        adapter = MeshtasticTextAdapter(radio_transport)
        self.service = AttachmentsService(
            self.conn,
            workspace_manager=self.workspace_manager,
            principal=self.principal,
            provider_registry=self.provider_registry,
            key_exchange=self.coordinator,
            connectivity_monitor=self.connectivity_monitor,
            delivery_adapter=adapter,
            # PR #231 review (2nd pass), requirement 4: this service's OWN
            # dedicated lock (self.tick_lock, constructed in __init__
            # above) - never this module's `_lock`. `self.conn` is
            # touched only by this service's own worker thread; `lock`
            # here is purely for tick()'s own re-entrancy (its own
            # docstring), a concern with no relation to the module-level
            # singleton lock any more. `self.inbound_queue` (not a
            # second, private one) is what the listener actually reaches
            # - thread-safe by construction (queue.Queue), needing no
            # lock of its own.
            lock=self.tick_lock,
            inbound_queue=self.inbound_queue,
            # Step 1.6A.1 (correction #1): hand the worker its *own* copies
            # of the command queue/registry/dispatcher/snapshot publisher/
            # wake_event - the exact same single instances this state also
            # handed the facade (see __init__), so a facade.submit() from a
            # request thread and this worker's drain both touch the same
            # queue/registry/Event, and the snapshot the worker publishes
            # is the snapshot the facade reads.
            command_queue=self.command_queue,
            command_registry=self.command_registry,
            # Step 1.6A.3A: hand the service the real lifecycle-command
            # dispatcher unless a test already replaced the placeholder with
            # a custom table (then honor that). Passing None makes the
            # service build its own from its handler methods - the handlers
            # are bound methods, so they can only be built here, once the
            # service and its worker-owned collaborators exist.
            dispatcher=(
                self.dispatcher
                if self.dispatcher is not self._dispatcher_placeholder
                else None
            ),
            snapshot_publisher=self.snapshot_publisher,
            wake_event=self.wake_event,
            ready_event=self.ready_event,
            pending_reservations=self.pending_reservations,
            recipient_snapshot_publisher=self.recipient_snapshot_publisher,
        )
        if self.dispatcher is self._dispatcher_placeholder:
            # The service built its real lifecycle-command dispatcher (we
            # passed None, so it fell back to _build_dispatcher()). Mirror it
            # back here so this state and the worker share the one fixed
            # kind->handler table - the invariant the runtime-wiring tests
            # assert (`svc._dispatcher is state.dispatcher`).
            self.dispatcher = self.service._dispatcher
        # Deliberately network_available=False and no relay_client/
        # delivery_adapter override for this *synchronous* startup pass:
        # this runs before the worker thread (and its Lock-held tick
        # discipline) exists at all, so it stays local-only - unsticking
        # whatever a restart left mid-state at the DB level (ADR-0008's
        # "restart never loses a job" guarantee) without making any
        # network call from the caller's thread. `service.start()` right
        # below does an immediate first tick, which is what actually
        # resumes uploads/downloads with a real, per-row relay client.
        sender.resume_pending(
            self.conn,
            workspace_manager=self.workspace_manager,
            principal=self.principal,
            network_available=False,
        )
        receiver.reconcile_pending(
            self.conn,
            workspace_manager=self.workspace_manager,
            principal=self.principal,
            provider_registry=self.provider_registry,
            key_exchange=self.coordinator,
            network_available=False,
        )
        self.service.start()
        return self.service


def _get_state(data_dir: str) -> "_MCARuntimeState":
    global _state
    with _lock:
        if _state is None:
            _state = _MCARuntimeState(data_dir)
        return _state


def start_attachments_service(data_dir: str, radio_transport: RadioTransport) -> None:
    """Called once from server.py's `start_runtime()`, alongside its
    existing `threading.Thread(target=..., daemon=True).start()` calls
    for `radio_health_worker` etc. (ADR-0008 decision 1's "Startup
    sequence"). Safe to call more than once (e.g. a hypothetical future
    profile-swap re-init path) - `ensure_service()` is idempotent and
    self-locking (its own docstring)."""
    state = _get_state(data_dir)
    state.ensure_service(radio_transport)


def get_attachments_facade() -> Optional[AttachmentsFacade]:
    """The request-facing facade, or `None` if the runtime has not been
    initialized yet (Step 1.6A.1 correction #1's "never lazy-create the
    SQLite runtime" guarantee). This is the one function Step 1.6A's REST
    layer calls to reach the attachments read/write surface; a `None` here
    is the explicit "not ready" signal a handler maps to 503/404 - it
    never falls back to initializing the database or worker from a request
    thread (§3.2).

    Reads the module-level `_state` directly (a single reference read,
    safe without a lock under the GIL - the same reasoning as
    `handle_incoming_meshtastic_text()`), and returns the facade that
    state built at construction time. Deliberately does **not** call
    `_get_state()`, which would create the SQLite runtime (and open
    `attachments.db`) from whatever thread happened to ask."""
    state = _state
    if state is None:
        return None
    return state.facade


def reset_state_for_tests() -> None:
    """Test-only: drop the process-lifetime singleton so a test can
    re-initialize against a fresh temp data_dir. Not called anywhere in
    production code.

    Stops `AttachmentsService`'s daemon thread (if `ensure_service()` was
    ever called on this state) before closing the connection - closing
    a still-ticking worker's connection out from under it would otherwise
    surface as sporadic "Cannot operate on a closed database" noise in
    whichever test happens to be running next, not in the test that
    actually caused it.

    PR #231 review, section 2/3: never deadlocks - `AttachmentsService.
    stop()` itself is bounded (`join(timeout=...)`) and now returns
    whether the worker actually stopped. If it did not (a pathologically
    slow tick still running after the timeout - not expected in normal
    operation), `conn` is deliberately NOT closed out from under it; this
    function still clears the module-level `_state` singleton so the
    *next* call to `_get_state()` builds a fresh `_MCARuntimeState` (and,
    with it, a fresh connection) rather than reusing one whose owning
    thread might still be alive. The old, still-running worker and its
    connection are leaked in that rare case, not corrupted.

    Also clears `set_connectivity_session_for_tests()`'s override back
    to `None`, so a test that set one does not silently leak a fake
    session into a later, unrelated test."""
    global _state, _test_connectivity_session_override
    with _lock:
        if _state is not None:
            stopped = True
            if _state.service is not None:
                stopped = _state.service.stop()
            if stopped:
                _state.conn.close()
        _state = None
        _test_connectivity_session_override = None


def handle_incoming_meshtastic_text(
    text: str,
    source_address: str,
    radio_transport: RadioTransport,
    *,
    data_dir: str,
    packet_id: Optional[str] = None,
) -> bool:
    """Called by server.py's listener immediately after a normal
    incoming direct-message text has already been saved (spec 19.1:
    never block the radio listener on parsing/crypto/Relay - the
    O(1) `text.startswith("MCA1:")` check happens in the caller,
    *before* this function is ever reached, so this function itself
    only runs for messages that already passed that cheap filter).

    PR #231 review (2nd pass), requirement 2: after `start_runtime()`'s
    own startup sequence has run (`server.py` now calls
    `start_attachments_service()` before starting the radio listener at
    all - see that call site's own comment), this function performs NO
    initialization, acquires NO singleton or tick lock, and makes NO
    SQLite/filesystem/network access of its own. It only ever reads the
    already-built module-level `_state` (a single reference read - safe
    without a lock under the GIL, and this module never mutates `_state`
    except at startup/test-reset, both of which happen-before any real
    listener call) and, if `_state`/`_state.service` already exist, hands
    an immutable `InboundEvent` straight to `AttachmentsService.
    enqueue_inbound()` - itself lock-free (a `queue.Queue.put_nowait()`
    plus a `threading.Event.set()`, both thread-safe by construction).

    `radio_transport` and `data_dir` are still accepted (server.py's
    existing call site already passes both) but are no longer used to
    initialize anything here - only `start_attachments_service()` (the
    startup-time call) does that. Kept in this signature rather than
    removed so server.py's call site does not need to branch on which
    code path it's talking to.

    Requirement 3 (fail fast, never initialize from the listener
    thread): if `_state`/`_state.service` is not yet built - should never
    happen in production given the startup ordering above, but a test or
    a future call site could still reach this function too early - the
    event is logged and dropped, exactly like a full inbound queue. This
    function never falls back to calling `_get_state()`/`ensure_service()`
    itself; that would reintroduce exactly the "listener thread
    initializes the database/worker" hazard requirement 3 rules out, on
    top of the shared-lock hazard the rest of this module's docstring
    already recounts.

    Returns True if the event was queued, False if it was dropped
    because the queue was full or the runtime was not ready yet -
    purely informational for the caller's own logging (server.py
    currently discards it), callers don't need to branch on it.

    Never raises: an unready runtime is handled internally (returns
    False, does not raise), and `enqueue_inbound()` itself cannot raise
    either. server.py's own call site still wraps this whole call in a
    try/except defensively, but nothing in this function's own body is
    expected to reach it.
    """
    state = _state
    if state is None or state.service is None:
        logger.warning(
            "handle_incoming_meshtastic_text: MCA runtime not ready yet (source=%s) - "
            "dropping event rather than initializing from the listener thread",
            source_address,
        )
        return False
    event = InboundEvent(
        text=text, source_address=source_address, packet_id=packet_id, received_at=time.time()
    )
    return state.service.enqueue_inbound(event)
