"""Tests for adapters/meshtastic/_timeout_support.py's TimeoutEnforced
mixin directly (not through SerialTransport/BLETransport) - the shared
watchdog used by both concrete transports.

Regression coverage for the Task 47 live finding on TAP2: a forced BLE
switch to a nonexistent address raised the underlying library's own
exception type (not TransportError) from inside the watchdog thread,
which _call_with_timeout used to re-raise unmodified. Every caller up
the stack - connect()'s own `except TransportError`, api_meshtastic.py's
fail-closed switch() recovery - only ever catches TransportError, so the
unwrapped exception silently bypassed the entire recovery path and
landed in Flask's generic error handler instead, leaving both
transports down with nothing having attempted recovery.
"""
import pytest

from adapters.meshtastic._timeout_support import TimeoutEnforced
from meshsrv.radio_transport import TransportError, TransportErrorCode


class _Watchdog(TimeoutEnforced):
    def __init__(self):
        super().__init__(thread_name_prefix="test-watchdog")


def test_arbitrary_exception_is_wrapped_as_transport_error_unknown():
    watchdog = _Watchdog()

    def _raises_something_unrelated():
        raise ValueError("device not found: not a TransportError at all")

    with pytest.raises(TransportError) as excinfo:
        watchdog._call_with_timeout(_raises_something_unrelated, timeout=5, what="test op")

    assert excinfo.value.code == TransportErrorCode.UNKNOWN
    assert "test op failed" in str(excinfo.value)
    assert "device not found" in str(excinfo.value)


def test_transport_error_raised_by_fn_passes_through_unwrapped():
    watchdog = _Watchdog()
    original = TransportError(TransportErrorCode.DEVICE_NOT_FOUND, "no such device")

    def _raises_transport_error():
        raise original

    with pytest.raises(TransportError) as excinfo:
        watchdog._call_with_timeout(_raises_transport_error, timeout=5, what="test op")

    # Must be the exact same TransportError, not double-wrapped into
    # another TransportError(UNKNOWN, "...TransportError(...)...").
    assert excinfo.value is original
    assert excinfo.value.code == TransportErrorCode.DEVICE_NOT_FOUND


def test_real_timeout_still_raises_timeout_not_unknown():
    import time

    watchdog = _Watchdog()

    def _slow():
        time.sleep(0.5)
        return "done"

    with pytest.raises(TransportError) as excinfo:
        watchdog._call_with_timeout(_slow, timeout=0.1, what="slow op")

    assert excinfo.value.code == TransportErrorCode.TIMEOUT


def test_successful_call_returns_its_value_unmodified():
    watchdog = _Watchdog()

    result = watchdog._call_with_timeout(lambda: {"ok": True}, timeout=5, what="fast op")

    assert result == {"ok": True}
