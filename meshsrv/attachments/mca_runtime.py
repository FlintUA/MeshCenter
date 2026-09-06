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

from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.base import DeliveryError, Route, RouteType
from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
from meshsrv.attachments.identity import MCAPrincipal, ensure_principal
from meshsrv.attachments.key_exchange import KeyExchangeCoordinator, RateLimited
from meshsrv.attachments.workspace import MCAWorkspaceManager
from meshsrv.radio_transport import RadioTransport

WORKSPACE_ID = "local"
ADAPTER_ID = "meshtastic"

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


def _get_state(data_dir: str) -> "_MCARuntimeState":
    global _state
    with _lock:
        if _state is None:
            _state = _MCARuntimeState(data_dir)
        return _state


def reset_state_for_tests() -> None:
    """Test-only: drop the process-lifetime singleton so a test can
    re-initialize against a fresh temp data_dir. Not called anywhere in
    production code."""
    global _state
    with _lock:
        if _state is not None:
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
    """
    state = _get_state(data_dir)
    adapter = MeshtasticTextAdapter(radio_transport)
    transport_event = {"text": text, "source_address": source_address, "packet_id": packet_id}

    with _lock:
        envelope = adapter.ingest(transport_event)
        if envelope is None:
            return False
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
