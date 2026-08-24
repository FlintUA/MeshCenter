"""Shared tier-1 timeout enforcement (docs/BACKEND_API.md "Timeouts") for
every RadioTransport implementation in this package.

Extracted from adapters/meshtastic/serial_transport.py (Task 44) during
Task 45, so BLETransport doesn't duplicate the same watchdog pattern - and
so a future TCP transport has it for free too. Behavior is unchanged: see
tests/test_serial_transport_timeout.py, which still exercises this exact
code through SerialTransport and is required to pass unmodified after this
extraction.

HOTFIX (caught live during Task 45's BLETransport smoke test, affects the
already-deployed SerialTransport too - see
test_call_with_timeout_thread_is_a_daemon_and_does_not_block_process_exit):
this originally used concurrent.futures.ThreadPoolExecutor. Its worker
threads are NOT daemon threads, and concurrent.futures.thread registers an
atexit hook (_python_exit()) that waits for every worker thread of every
ThreadPoolExecutor ever created, regardless of that executor's own
shutdown() state, before the process is allowed to exit. A genuinely
hung tier-1-abandoned call (live-observed: BLEInterface.close()'s
self._eventThread.join() with no timeout of its own) therefore blocked
clean process exit - not "wait longer", an unconditional hang requiring
kill -9. A prior review suggested overriding ThreadPoolExecutor's private
_thread_class attribute to mark workers as daemon threads; that attribute
does not exist on this project's Python version (3.14 - confirmed via
`hasattr(ThreadPoolExecutor(), '_thread_class')` == False), so this
module does not use ThreadPoolExecutor at all: each call gets its own
plain `threading.Thread(daemon=True)`, resulted via a one-slot
`queue.Queue`, with no pooling and no private API surface to break on a
future Python version.
"""
from __future__ import annotations

import queue
import threading
from typing import Callable

from meshsrv.radio_transport import TransportError, TransportErrorCode


class TimeoutEnforced:
    """Mixin providing _call_with_timeout(). Non-blocking return to the
    caller only - see docs/BACKEND_API.md "Timeouts" for why this does
    NOT guarantee the wrapped call actually stops (tier 1 vs tier 2)."""

    def __init__(self, thread_name_prefix: str) -> None:
        self._timeout_thread_name_prefix = thread_name_prefix
        self._timeout_call_counter = 0
        self._timeout_call_counter_lock = threading.Lock()

    def _call_with_timeout(self, fn: Callable[[], object], timeout: float, what: str):
        result_box: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

        def _run():
            try:
                result_box.put(("ok", fn()))
            except BaseException as exc:  # noqa: BLE001 - deliberately catch-all, re-raised on the caller's thread below
                result_box.put(("error", exc))

        with self._timeout_call_counter_lock:
            self._timeout_call_counter += 1
            thread_name = f"{self._timeout_thread_name_prefix}-{self._timeout_call_counter}"

        # daemon=True is the entire point of this module: if `fn` never
        # returns (the tier-2 gap documented on every transport's
        # connect()/disconnect()), this thread is abandoned exactly like
        # before, but a daemon thread never blocks process exit - see
        # this module's HOTFIX docstring.
        thread = threading.Thread(target=_run, name=thread_name, daemon=True)
        thread.start()

        try:
            status, payload = result_box.get(timeout=timeout)
        except queue.Empty:
            raise TransportError(
                TransportErrorCode.TIMEOUT, f"{what} exceeded {timeout}s"
            ) from None

        if status == "error":
            # HOTFIX (live Task 47 finding on TAP2): this used to `raise
            # payload` unconditionally, re-raising whatever `fn` happened
            # to throw - fine for TransportError (raised deliberately by
            # a transport's own code), but any OTHER exception type
            # (e.g. BLEInterface() raising bleak's own "device not
            # found" error) came back to the caller in its raw,
            # un-typed form. Every caller up the stack - connect()'s own
            # `except TransportError`, api/api_meshtastic.py's fail-
            # closed switch() recovery - only ever catches TransportError,
            # so an unwrapped exception skipped all of that and reached
            # Flask's generic error handler instead, silently bypassing
            # the entire recovery path this was built for. Live-caught:
            # a forced switch to a nonexistent BLE address left both
            # transports down with no recovery attempt, because the
            # "device not found" exception was never a TransportError to
            # begin with. Wrap anything that isn't already one.
            if isinstance(payload, TransportError):
                raise payload
            raise TransportError(
                TransportErrorCode.UNKNOWN, f"{what} failed: {payload}"
            ) from payload
        return payload

    def _shutdown_executor(self) -> None:
        """No pooled executor to shut down anymore (see this module's
        HOTFIX docstring) - kept as a no-op so close() in every transport
        (which calls this) doesn't need its own version check."""
