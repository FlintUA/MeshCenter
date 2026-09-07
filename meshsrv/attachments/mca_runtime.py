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

PR #231 review, section 2 (single-owner SQLite model): this module's
own `_lock`/`state.conn` are no longer touched by
`handle_incoming_meshtastic_text()` at all. That function now does
exactly one thing - build an immutable `service.InboundEvent` from the
raw text/metadata and hand it to `AttachmentsService.enqueue_inbound()`
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

If `AttachmentsService` has not started yet (the narrow window between
process start and `start_attachments_service()` completing, or if it
failed to start), events still queue safely - `_MCARuntimeState.__init__`
constructs the bounded queue eagerly, before any listener thread could
plausibly call this function, and `ensure_service()` hands that same
queue object to the real `AttachmentsService` once it exists (not a
second, throwaway queue) - so nothing queued during that window is
lost, only delayed until the worker starts draining it.

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

import queue
import sqlite3
import threading
import time
from typing import Optional

from meshsrv.attachments import receiver, sender
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
from meshsrv.attachments.identity import MCAPrincipal, ensure_principal
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.service import INBOUND_QUEUE_MAXSIZE, AttachmentsService, InboundEvent
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor
from meshsrv.radio_transport import RadioTransport

WORKSPACE_ID = "local"
ADAPTER_ID = "meshtastic"

# Guards only this module's own singleton bookkeeping (_state creation/
# reset) - NOT database access any more (PR #231 review, section 2: the
# radio listener thread no longer touches `conn` at all, so there is no
# second accessor left to serialize against; see AttachmentsService's own
# `_lock` for tick() re-entrancy, a separate, narrower concern).
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
        module per-call, via `handle_incoming_meshtastic_text()`'s own
        parameter, never as something stored at process startup. Callers
        pass it in explicitly (`start_attachments_service()` below).

        Idempotent and self-locking (acquires the module-level `_lock`
        internally, rather than requiring every caller to remember to -
        PR #231 review found this module's own `handle_incoming_
        meshtastic_text()` calling this method without holding `_lock` at
        all, a real bug introduced while wiring the queue-based redesign
        in, fixed here by moving the lock inside this method instead of
        trusting each call site) so a second call - whether a second real
        startup attempt or an incoming message racing server.py's own
        startup call - just returns the already-running instance rather
        than building a second worker thread or re-running
        resume_pending()/reconcile_pending() a second time.
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
            # PR #231 review, section 2: `self.conn` is now touched only
            # by this service's own worker thread - `lock` here is just
            # for tick()'s own re-entrancy (its own docstring), not for
            # serializing against the radio listener, which no longer
            # accesses `conn` at all. `self.inbound_queue` (not a second,
            # private one) is what the listener actually reaches -
            # thread-safe by construction (queue.Queue), needing no lock
            # of its own.
            lock=_lock,
            inbound_queue=self.inbound_queue,
        )
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

    PR #231 review, section 2: this function does exactly two things -
    build an immutable `InboundEvent` and hand it to
    `AttachmentsService.enqueue_inbound()` - and nothing else. No CBOR
    decode, no signature verification, no database access, no network
    call happens here or anywhere this function calls into; all of that
    now happens later, on `AttachmentsService`'s own worker thread (see
    `service.py`'s `_drain_inbound_events()`/`_process_one_inbound_event()`).
    `radio_transport` is still accepted (server.py's existing call site
    already passes it, and `start_attachments_service()`/`ensure_service()`
    need it to build the one `MeshtasticTextAdapter` the worker thread
    sends replies through) but this function itself never calls anything
    on it - ingest/encode/send all move to the worker side too.

    Returns True if the event was queued, False if it was dropped
    because the queue was full (`AttachmentsService.enqueue_inbound()`'s
    own return value) - purely informational for the caller's own
    logging (server.py currently discards it), callers don't need to
    branch on it.

    Never raises: `ensure_service()`/`_get_state()` themselves are the
    only things that could plausibly fail here (e.g. a filesystem
    error), and server.py's own call site already wraps this whole call
    in a try/except for exactly that reason - this function does not
    duplicate that guard internally, to avoid silently swallowing a
    real startup-path failure that caller wants to see and log itself.
    """
    state = _get_state(data_dir)
    service = state.ensure_service(radio_transport)
    event = InboundEvent(
        text=text, source_address=source_address, packet_id=packet_id, received_at=time.time()
    )
    return service.enqueue_inbound(event)
