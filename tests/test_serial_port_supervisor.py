"""Tests for meshsrv/serial_port_supervisor.py's SerialPortSupervisor -
extracted from adapters/meshtastic/serial_transport.py's SerialTransport
during the P0 #1 stabilization follow-up (independent audit). Covers the
listener-management + exclusive-access-claim surface
(run_listener()/get_listener_pid()/claim_exclusive_access()/the internal
stop_listener_process()/wait_serial_release() pair) moved 1:1 out of that
class - see meshsrv/serial_port_supervisor.py's own module docstring for
why this is a shared primitive, not Core-exclusive logic.

No server.py import needed - SerialPortSupervisor takes radio_lock/
pause_listen by DI and never imports server.py or meshtastic itself.
"""
import threading
import time

from meshsrv.serial_port_supervisor import SerialPortSupervisor


def _make_supervisor():
    return SerialPortSupervisor(
        cli_path="/does/not/matter/for/this/test",
        port="/dev/ttyFAKE",
        radio_lock=threading.RLock(),
        pause_listen=threading.Event(),
    )


# --- Task 49: get_listener_pid()'s bounded radio_lock acquire -------------
# (moved from tests/test_serial_transport_timeout.py - unchanged behavior,
# now exercised against SerialPortSupervisor directly rather than through
# SerialTransport)


def test_get_listener_pid_returns_none_on_a_busy_lock_within_its_short_budget():
    """Task 49 fix: once claim_exclusive_access() can hold this same
    radio_lock for a full IPC round-trip, get_listener_pid()'s old
    unconditional `with self._radio_lock:` could stall the whole
    dashboard page behind it. Now bounded by
    _LISTENER_PID_LOCK_TIMEOUT_S (3.0s, reusing meshsrv/transport_router.
    py's own _INFO_LOCK_TIMEOUT_S precedent) and, on a busy lock, returns
    None - a synthetic fallback matching is_connected()'s same fail-safe
    pattern, not a raised exception - a passive status field degrading
    gracefully, not an action failing loudly."""
    supervisor = _make_supervisor()
    supervisor._radio_lock.acquire()
    try:
        start = time.monotonic()
        result = supervisor.get_listener_pid()
        elapsed = time.monotonic() - start
    finally:
        supervisor._radio_lock.release()

    assert result is None
    assert elapsed < supervisor._LISTENER_PID_LOCK_TIMEOUT_S + 1.0, (
        f"took {elapsed:.2f}s - should give up within its own short budget, not block indefinitely"
    )


def test_get_listener_pid_still_works_normally_when_the_lock_is_free():
    """Sanity check alongside the busy-lock test above: the bounded
    acquire must not change behavior in the ordinary, uncontended case -
    still reports None when there's genuinely no listener process (this
    instance never runs run_listener() in these tests)."""
    supervisor = _make_supervisor()
    assert supervisor.get_listener_pid() is None


# --- claim_exclusive_access() - port-busy path -----------------------------


def test_claim_exclusive_access_raises_busy_when_the_port_never_frees():
    """claim_exclusive_access() (renamed from claim_for_external_command()/
    _claim_radio() during this stabilization follow-up) must still raise
    TransportError(BUSY) when prepare_command() reports the port never
    freed up - unchanged behavior from before the move, just relocated.
    wait_serial_release() is stubbed to always report "still busy" so
    this doesn't depend on real `lsof`/OS state."""
    from meshsrv.radio_transport import TransportError, TransportErrorCode

    supervisor = _make_supervisor()
    supervisor.wait_serial_release = lambda timeout=8: False

    try:
        with supervisor.claim_exclusive_access(timeout=0.2, cooldown=0):
            raise AssertionError("should never reach the yield - the port never freed")
    except TransportError as error:
        assert error.code == TransportErrorCode.BUSY
    else:
        raise AssertionError("claim_exclusive_access() did not raise on a busy port")
