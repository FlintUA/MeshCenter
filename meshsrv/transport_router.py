"""TransportRouter — the one stable RadioTransport every DI consumer
(api/api_chat.py, api/api_waypoints.py, meshsrv/schedule_actions.py) is
wired to, so switching the active concrete transport (Serial <-> BLE) at
runtime (Task 46's POST /api/meshtastic/transport) never requires
re-wiring any consumer - meshsrv/*.py modules never do `from server
import ...` (server.py imports FROM meshsrv, a reverse import risks a
circular import - see meshsrv/schedule_engine.py's docstring for the
established reasoning), so a module-level "current transport" global in
server.py that consumers reach into was never an option; this router is
the alternative.

Does not itself talk to meshtastic - it only dispatches to whichever
concrete RadioTransport (SerialTransport/BLETransport,
adapters/meshtastic/) is currently active, so it lives in meshsrv/, not
adapters/meshtastic/.
"""
from __future__ import annotations

import threading
from typing import Callable

from meshsrv.radio_transport import RadioTransport


class TransportRouter(RadioTransport):
    """LOCKING (per review, ahead of implementation): self._lock covers
    the ENTIRE delegated call, not just the read of self._active - and
    switch() runs its whole disconnect-old/connect-new/reassign sequence
    under the same lock. The node accepts only one active link (serial OR
    BLE) at a time - live-confirmed twice in Task 43 (a BLE connect
    failed until the USB cable was physically removed). A send_*/get_*
    call reaching the old transport mid-teardown while the new one is
    mid-connect would race for the same physical radio at the firmware
    level, not just in Python - locking only the pointer read/write would
    not prevent that. A call landing during a switch blocks for as long
    as the new transport's connect() takes (live-measured BLE: up to
    ~90s) - a predictable wait, not a race that needs diagnosing later.
    Same principle as SerialTransport._prepare_radio_command()'s fix in
    Task 44."""

    def __init__(self, initial: RadioTransport) -> None:
        self._lock = threading.Lock()
        self._active = initial

    def switch(self, connect_new: Callable[[], RadioTransport]) -> RadioTransport:
        """`connect_new` is a zero-arg callable provided by the caller
        (server.py's transport-switch handler) that disconnects whatever
        needs disconnecting and returns an already-connect()-ed new
        transport. Runs entirely under self._lock - see class docstring.

        If connect_new() raises, self._active is NOT changed - but the
        old transport it still points to may already be disconnected by
        that point, if connect_new() disconnects it before attempting
        the new connect (the normal Serial->BLE sequence does exactly
        this). This method only guarantees self._active's correctness,
        not that whatever it still points to is connected - the caller
        (server.py) is responsible for reconnecting the old transport on
        a failed switch if it wants to leave the system usable."""
        with self._lock:
            old = self._active
            new = connect_new()
            self._active = new
        return old

    def _delegate(self, name: str, *args, **kwargs):
        with self._lock:
            return getattr(self._active, name)(*args, **kwargs)

    # ------------------------------------------------------------------
    # RadioTransport - every method delegates to whichever concrete
    # transport is currently active, under self._lock.
    # ------------------------------------------------------------------
    def connect(self, *args, **kwargs):
        return self._delegate("connect", *args, **kwargs)

    def disconnect(self, *args, **kwargs):
        return self._delegate("disconnect", *args, **kwargs)

    def reconnect(self, *args, **kwargs):
        return self._delegate("reconnect", *args, **kwargs)

    def is_connected(self):
        return self._delegate("is_connected")

    def send_text(self, *args, **kwargs):
        return self._delegate("send_text", *args, **kwargs)

    def send_packet(self, *args, **kwargs):
        return self._delegate("send_packet", *args, **kwargs)

    def send_messages(self, *args, **kwargs):
        return self._delegate("send_messages", *args, **kwargs)

    def send_waypoint(self, *args, **kwargs):
        return self._delegate("send_waypoint", *args, **kwargs)

    def get_nodes(self, *args, **kwargs):
        return self._delegate("get_nodes", *args, **kwargs)

    def get_local_node(self, *args, **kwargs):
        return self._delegate("get_local_node", *args, **kwargs)

    def get_channels(self, *args, **kwargs):
        return self._delegate("get_channels", *args, **kwargs)

    def get_metadata(self, *args, **kwargs):
        return self._delegate("get_metadata", *args, **kwargs)

    def set_device_time(self, *args, **kwargs):
        return self._delegate("set_device_time", *args, **kwargs)

    def get_connection_info(self):
        return self._delegate("get_connection_info")

    def close(self):
        return self._delegate("close")
