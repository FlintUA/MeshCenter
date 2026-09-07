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
`AttachmentsService`) and, in `handle_incoming_meshtastic_text()`,
routes an inbound OFFER frame to `receiver.handle_offer()` -
previously this function only ever reached `KeyExchangeCoordinator`,
so a real incoming attachment offer over the radio had no consumer at
all in production (only in tests calling `receiver.handle_offer()`
directly). `ProviderRegistry`/`ConnectivityMonitor` are constructed
eagerly in `_MCARuntimeState.__init__` (cheap - no thread, no network
I/O until `refresh()`/`service.start()` are explicitly called), so an
OFFER can be handled correctly even before `start_attachments_service()`
below has run; `AttachmentsService` itself is *not* constructed there,
because it needs `radio_transport` (only ever handed to us per-call by
`handle_incoming_meshtastic_text()`'s caller, never stored) to build
the one `MeshtasticTextAdapter` it sends through - see
`start_attachments_service()`'s own docstring.

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

import sqlite3
import threading
from typing import Optional

from meshsrv.attachments import codec, receiver, sender
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.base import DeliveryError, Route, RouteType
from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
from meshsrv.attachments.identity import MCAPrincipal, ensure_principal
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator, RateLimited
from meshsrv.attachments.provider_registry import ProviderRegistry
from meshsrv.attachments.service import AttachmentsService
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.connectivity_monitor import ConnectivityMonitor, InternetStatus
from meshsrv.radio_transport import RadioTransport

WORKSPACE_ID = "local"
ADAPTER_ID = "meshtastic"

# The single lock for all access to `_MCARuntimeState.conn` (ADR-0008-
# hardening, PR #227 defect #2: this used to guard only this module's own
# singleton bookkeeping and direct-write call sites, while AttachmentsService
# separately built and held its own private Lock over the very same
# connection - two locks, one connection, no real mutual exclusion between
# the radio listener thread and the service's worker thread). Handed to
# AttachmentsService's constructor in ensure_service() below so both sides
# serialize on this one instance - this module is the connection's single
# owner, everyone else borrows the lock, never a copy of it.
_lock = threading.Lock()
_state: "Optional[_MCARuntimeState]" = None


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
        self.connectivity_monitor = ConnectivityMonitor(self.provider_registry)
        # Unlike the two above, the worker thread itself is not started
        # until ensure_service() runs - see that method's own docstring
        # for why it needs radio_transport, which this constructor never
        # receives.
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

        Idempotent and lock-guarded by the caller (both current callers
        already hold `_lock` when they call this) so a second call -
        whether a second real startup attempt or an incoming message
        racing server.py's own startup call - just returns the already-
        running instance rather than building a second worker thread or
        re-running resume_pending()/reconcile_pending() a second time.
        """
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
            # Reviewer-found defect (PR #227 defect #2): `self.conn` is
            # also written to directly by handle_incoming_meshtastic_text()
            # below (coordinator.handle_incoming()/receiver.handle_offer()),
            # which runs on the radio listener thread, not this service's
            # own worker thread. Passing this module's own `_lock` here -
            # the same lock handle_incoming_meshtastic_text() already
            # holds for every direct write it makes - means AttachmentsService.
            # tick() and the listener's direct writes now serialize on one
            # single lock instead of two independent ones that each only
            # ever protected their own call site. See AttachmentsService.
            # __init__()'s own docstring/comment for the full defect.
            lock=_lock,
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
    profile-swap re-init path) - `ensure_service()` is idempotent."""
    state = _get_state(data_dir)
    with _lock:
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
    actually caused it."""
    global _state
    with _lock:
        if _state is not None:
            if _state.service is not None:
                _state.service.stop()
            _state.conn.close()
        _state = None


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

    Returns True if `text` was recognized as an MCA1-TEXT message
    (whether or not a reply was actually sent), False if `ingest()`
    decided it wasn't MCA after all - purely informational for the
    caller's own logging, callers don't need to branch on it.

    Never raises: a malformed/hostile MCA payload, a rate-limited
    KEY_REQUEST, or a failed reply-send must not take down the radio
    listener thread - each failure mode is caught and logged here,
    the same "best-effort, never crash the listener" contract every
    other block in server.py's process_message_line() already follows.

    ADR-0008: an OFFER frame is routed to `receiver.handle_offer()`
    instead of `coordinator.handle_incoming()` - the latter only ever
    dispatches KEY_REQUEST/KEY_ANNOUNCE/KEY_ACK (see its own
    `codec.peek_message_type()`-based dispatch) and returns None for
    anything else, so an OFFER reaching it before this change was
    silently dropped with no attachment row ever created. Every other
    message type (ACK_RECEIVED/ACK_DOWNLOADED/ACK_PROVIDER_UNKNOWN/
    CANCEL/REJECTED/EXPIRED/KEY_ROTATE) still falls through to
    `coordinator.handle_incoming()`, which returns None for those today
    - a pre-existing gap flagged, not fixed, here: `sender.py`'s own
    module docstring already documents that nothing yet drives
    `on_ack_received()`/`on_ack_downloaded()` from a real incoming wire
    message (no production caller exists for either), so the sender
    side's SENT->RECEIVED->DOWNLOADED progression does not yet advance
    off of a real ACK on the wire. Out of scope for ADR-0008 (which
    does not touch `sender.py`'s or `receiver.py`'s signatures at all)
    and for this wiring pass; revisit as its own reviewed change, since
    it involves verifying a signed wire payload, not just routing one.
    """
    state = _get_state(data_dir)
    adapter = MeshtasticTextAdapter(radio_transport)
    transport_event = {"text": text, "source_address": source_address, "packet_id": packet_id}

    with _lock:
        envelope = adapter.ingest(transport_event)
        if envelope is None:
            return False

        try:
            message_type = codec.peek_message_type(envelope.logical_message)
        except codec.CodecError as exc:
            print(f"[MCA] malformed MCA message from {source_address}: {exc}", flush=True)
            return True

        if message_type == codec.MessageType.OFFER:
            # Fail-open, same policy as ConnectivityMonitor.can_attempt_relay():
            # this only decides which of the two equally-automatic starting
            # states (WAITING_CONSENT vs WAITING_NETWORK) the new attachment
            # begins in - AttachmentsService's own tick (woken just below)
            # re-evaluates it within one tick regardless, so getting this
            # guess wrong for an unknown/never-checked internet state costs
            # nothing beyond one extra tick.
            network_available = state.connectivity_monitor.snapshot().internet != InternetStatus.OFFLINE
            try:
                receiver.handle_offer(
                    state.conn,
                    workspace_manager=state.workspace_manager,
                    principal=state.principal,
                    provider_registry=state.provider_registry,
                    key_exchange=state.coordinator,
                    raw_offer=envelope.logical_message,
                    network_available=network_available,
                    # PR #227 defect #1: this is the only place a
                    # 'received' attachment's reply route is ever learned
                    # - persisted on the attachment row so AttachmentsService's
                    # dispatch step can actually send ACK_RECEIVED/
                    # ACK_PROVIDER_UNKNOWN/ACK_DOWNLOADED back later,
                    # possibly ticks (or a restart) after this call returns.
                    source_address=source_address,
                )
            except receiver.ReceiverError as exc:
                print(f"[MCA] rejected OFFER from {source_address}: {exc}", flush=True)
                return True
            if state.service is not None:
                state.service.wake()
            return True

        try:
            reply_logical = state.coordinator.handle_incoming(envelope)
        except RateLimited as exc:
            print(f"[MCA] KEY_REQUEST from {source_address} rate-limited: {exc}", flush=True)
            return True
        except DeliveryError as exc:
            print(f"[MCA] malformed MCA message from {source_address}: {exc}", flush=True)
            return True

    if reply_logical is None:
        return True

    try:
        route = Route(route_type=RouteType.DIRECT, route_id=source_address, destination_address=source_address)
        wire_payload = adapter.encode(reply_logical, route)
        adapter.send(wire_payload, route, idempotency_key=f"mca-reply-{packet_id or source_address}")
    except Exception as exc:  # noqa: BLE001 - must never crash the listener thread
        print(f"[MCA] failed to send reply to {source_address}: {exc}", flush=True)

    return True
