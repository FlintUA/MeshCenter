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
import time
from typing import Callable

from meshsrv.radio_transport import (
    ConnectionInfo,
    ConnectionState,
    RadioTransport,
    TransportError,
    TransportErrorCode,
)


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
    not prevent that.

    BOUNDED WAIT (Task 47.5, review correction): the lock-acquire itself
    used to be unconditional (`with self._lock:`), so a caller's own
    `timeout` argument only ever bounded the operation *after* the lock
    was acquired - it did not cover how long the call could sit waiting
    for the lock at all. A send_text(timeout=30) landing during a 135s
    switch() therefore actually waited up to 135+30=165s, not the 30s its
    own signature promised - live-caught (thread-pool exhaustion on
    dev/camtest, Task 47 item 3) because gunicorn's worker thread stays
    consumed by that entire real wait; `curl --max-time` on the client
    side does not cancel it.

    Every method below now does `self._lock.acquire(timeout=...)` and
    splits the caller's own already-existing `timeout` between the lock
    wait and the delegated operation - the TOTAL wall-clock time (lock
    wait + operation) is bounded by the same number the caller already
    asked for, not a new, separately-invented threshold. If the lock
    can't be acquired in time, raises TransportError(BUSY) - the caller
    gets a bounded, honest "try again" instead of an unbounded hang, and
    gunicorn releases the thread back to the pool.

    get_connection_info()/is_connected() have no `timeout` parameter at
    all per the ABC contract ("non-blocking... does not itself talk to
    the radio") and must not raise (also per contract) - they get a
    short, dedicated _INFO_LOCK_TIMEOUT_S and a synthetic fast response
    on a busy lock instead: get_connection_info() reports state=CONNECTING
    (a switch/connect genuinely is in progress) with the BUSY detail in
    last_error; is_connected() reports False (fail-safe - never claim
    connected when it can't be confirmed)."""

    # How long GET /api/meshtastic/connection (loadMeshtasticConnectionStatus()
    # polling) and is_connected() wait for the lock before giving up and
    # returning a synthetic "busy" response - these have no timeout
    # parameter of their own to split, so this is a dedicated constant.
    # 3.0s: short enough that routine UI polling never piles up into the
    # same thread-pool-exhaustion pattern that triggered this fix, long
    # enough to not misreport "busy" for an ordinary few-hundred-ms
    # delegated call under normal (non-switching) conditions.
    _INFO_LOCK_TIMEOUT_S = 3.0

    # switch() itself has no natural pre-existing `timeout` to split (it
    # takes a zero-arg connect_new() callable whose own duration is
    # entirely the caller's responsibility - see switch()'s docstring).
    # A second switch() landing while one is already in progress should
    # fail fast rather than queue for the first one's full ~90-135s only
    # to then run its own full duration on top.
    _SWITCH_LOCK_TIMEOUT_S = 5.0

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
        a failed switch if it wants to leave the system usable.

        Raises TransportError(BUSY) instead of blocking indefinitely if
        another switch or long-running delegated call already holds the
        lock - see class docstring's BOUNDED WAIT note."""
        if not self._lock.acquire(timeout=self._SWITCH_LOCK_TIMEOUT_S):
            raise TransportError(
                TransportErrorCode.BUSY,
                f"switch() could not acquire the router lock within {self._SWITCH_LOCK_TIMEOUT_S}s "
                "- another switch or long-running call is already in progress",
            )
        try:
            old = self._active
            new = connect_new()
            self._active = new
            return old
        finally:
            self._lock.release()

    def _delegate(self, name: str, *args, timeout: float, **kwargs):
        """Splits `timeout` (the caller's own already-existing budget for
        this operation) between the lock-acquire wait and the delegated
        call itself, so the TOTAL time this can hold a caller's thread is
        bounded by `timeout`, not `timeout` twice over. See class
        docstring's BOUNDED WAIT note - this is the Task 47.5 fix."""
        start = time.monotonic()
        if not self._lock.acquire(timeout=timeout):
            raise TransportError(
                TransportErrorCode.BUSY,
                f"{name}() could not acquire the router lock within {timeout}s "
                "- a switch or another long-running call is in progress",
            )
        try:
            remaining = max(0.0, timeout - (time.monotonic() - start))
            return getattr(self._active, name)(*args, timeout=remaining, **kwargs)
        finally:
            self._lock.release()

    # ------------------------------------------------------------------
    # RadioTransport - every method delegates to whichever concrete
    # transport is currently active, under self._lock. Each wrapper below
    # supplies the same default `timeout` as the ABC method it mirrors
    # (meshsrv/radio_transport.py), so an omitted timeout behaves exactly
    # as it did before this fix - only now that number is a real total
    # budget, not just the post-lock portion of one.
    # ------------------------------------------------------------------
    def connect(self, *args, timeout: float = 30.0, **kwargs):
        return self._delegate("connect", *args, timeout=timeout, **kwargs)

    def disconnect(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("disconnect", *args, timeout=timeout, **kwargs)

    def reconnect(self, *args, timeout: float = 30.0, **kwargs):
        return self._delegate("reconnect", *args, timeout=timeout, **kwargs)

    def send_text(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("send_text", *args, timeout=timeout, **kwargs)

    def send_packet(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("send_packet", *args, timeout=timeout, **kwargs)

    def send_messages(self, *args, timeout: float = 30.0, **kwargs):
        return self._delegate("send_messages", *args, timeout=timeout, **kwargs)

    def send_waypoint(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("send_waypoint", *args, timeout=timeout, **kwargs)

    def get_nodes(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("get_nodes", *args, timeout=timeout, **kwargs)

    def get_local_node(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("get_local_node", *args, timeout=timeout, **kwargs)

    def get_channels(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("get_channels", *args, timeout=timeout, **kwargs)

    def get_metadata(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("get_metadata", *args, timeout=timeout, **kwargs)

    def set_device_time(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("set_device_time", *args, timeout=timeout, **kwargs)

    def close(self, *args, timeout: float = 15.0, **kwargs):
        return self._delegate("close", *args, timeout=timeout, **kwargs)

    # ------------------------------------------------------------------
    # Non-blocking per the ABC contract - must not raise, must not wait
    # for the lock the same way the timeout-bearing methods above do.
    # ------------------------------------------------------------------
    def is_connected(self) -> bool:
        if not self._lock.acquire(timeout=self._INFO_LOCK_TIMEOUT_S):
            return False
        try:
            return self._active.is_connected()
        finally:
            self._lock.release()

    def get_connection_info(self) -> ConnectionInfo:
        if not self._lock.acquire(timeout=self._INFO_LOCK_TIMEOUT_S):
            return ConnectionInfo(
                state=ConnectionState.CONNECTING,
                descriptor=None,
                node_id=None,
                connected_since=None,
                last_error=TransportError(
                    TransportErrorCode.BUSY,
                    "status temporarily unavailable - a transport switch or "
                    "another long-running call is in progress",
                ),
            )
        try:
            return self._active.get_connection_info()
        finally:
            self._lock.release()
