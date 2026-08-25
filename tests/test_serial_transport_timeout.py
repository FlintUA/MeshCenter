"""Tests for adapters/meshtastic/serial_transport.py's tier-1 timeout
enforcement (_call_with_timeout) - specifically that concurrent calls do
not spuriously time each other out via the internal executor's queue.

No server.py import needed - SerialTransport takes radio_lock/pause_listen
by DI and never imports server.py itself.
"""
import threading
import time
from contextlib import contextmanager

import pytest

from adapters.meshtastic.serial_transport import SerialTransport
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionState,
    ConnectionType,
    OutgoingMessage,
    TransportError,
    TransportErrorCode,
)


def _make_transport():
    return SerialTransport(
        cli_path="/does/not/matter/for/this/test",
        port="/dev/ttyFAKE",
        radio_lock=threading.RLock(),
        pause_listen=threading.Event(),
    )


def test_single_call_exceeding_its_own_timeout_raises_timeout():
    transport = _make_transport()

    def slow():
        time.sleep(0.5)
        return "done"

    with pytest.raises(TransportError) as excinfo:
        transport._call_with_timeout(slow, timeout=0.1, what="slow()")
    assert excinfo.value.code == TransportErrorCode.TIMEOUT


def test_call_within_its_timeout_succeeds():
    transport = _make_transport()

    def fast():
        return "ok"

    assert transport._call_with_timeout(fast, timeout=1.0, what="fast()") == "ok"


def test_concurrent_calls_do_not_spuriously_timeout_each_other():
    """Regression test for the max_workers=1 queueing bug flagged in
    review: a long-running call must not eat into an unrelated shorter
    call's own timeout budget just because they were both submitted to
    the same executor. The first call is deliberately slower than the
    second call's own timeout - if the executor were still serialized
    behind a single worker, the second call would queue behind the first
    and raise TransportError(TIMEOUT) even though its own work is
    instant.
    """
    transport = _make_transport()
    started_second_at = []

    def long_running():
        time.sleep(0.6)
        return "long-done"

    def short_and_fast():
        started_second_at.append(time.monotonic())
        return "short-done"

    results = {}
    errors = {}

    def run_first():
        results["first"] = transport._call_with_timeout(long_running, timeout=5.0, what="long_running()")

    t0 = time.monotonic()
    first_thread = threading.Thread(target=run_first)
    first_thread.start()
    time.sleep(0.05)  # let the first call actually start executing

    try:
        results["second"] = transport._call_with_timeout(
            short_and_fast, timeout=0.2, what="short_and_fast()"
        )
    except TransportError as error:
        errors["second"] = error

    first_thread.join(timeout=5.0)

    assert "second" not in errors, (
        "short_and_fast() spuriously timed out - it queued behind long_running() "
        "instead of getting its own executor thread"
    )
    assert results.get("second") == "short-done"
    # The second call's own work must have started almost immediately,
    # not after waiting ~0.6s for the first call to finish.
    assert started_second_at[0] - t0 < 0.3
    assert results.get("first") == "long-done"


def test_call_with_timeout_thread_is_a_daemon_and_does_not_block_process_exit():
    """Regression test for the HOTFIX in _timeout_support.py: a call that
    never returns (the tier-2 gap - live-observed as BLEInterface.close()
    hanging past its own timeout during Task 45's TAP2 smoke test) must
    not require kill -9 to let the process exit. The old
    ThreadPoolExecutor-based implementation failed this - its worker
    threads are not daemon threads, and concurrent.futures.thread's
    atexit hook waits for them regardless of shutdown() state. Verified
    two ways: (1) the caller gets TransportError(TIMEOUT) back on time
    even though the underlying call never finishes, (2) the thread
    actually running that stuck call is introspected and confirmed
    daemon=True - not a full process fork, but the property that matters.
    """
    transport = _make_transport()
    thread_seen = {}
    release_event = threading.Event()

    def never_returns():
        thread_seen["thread"] = threading.current_thread()
        release_event.wait(timeout=5)  # keeps the thread alive briefly so
        # the assertion below can inspect it; does not affect the
        # timeout assertion, which already completed by then.
        return "should never be observed by the caller"

    start = time.monotonic()
    with pytest.raises(TransportError) as excinfo:
        transport._call_with_timeout(never_returns, timeout=0.2, what="never_returns()")
    elapsed = time.monotonic() - start

    assert excinfo.value.code == TransportErrorCode.TIMEOUT
    assert elapsed < 1.0, "the caller waited far longer than the requested timeout"

    # Give the abandoned thread a moment to actually start running (the
    # timeout above fires before it necessarily has).
    for _ in range(50):
        if "thread" in thread_seen:
            break
        time.sleep(0.02)
    assert "thread" in thread_seen, "the abandoned call never actually started"
    assert thread_seen["thread"].daemon is True, (
        "the thread running a hung call is not a daemon thread - it will "
        "block clean process exit exactly like the live TAP2 kill -9 case"
    )

    release_event.set()  # let the background thread finish so it's not
    # left dangling for the rest of the test session (best-effort - even
    # if it stayed stuck, being a daemon thread means it wouldn't hang
    # the test process's own exit).


def test_concurrent_connect_and_send_do_not_race_prepare_phase():
    """Regression test for the second review finding: with the executor
    no longer max_workers=1, connect(force=True)'s stop+wait teardown and
    send_text()'s _claim_radio() prepare phase could, before this fix,
    both be "in progress" at once (each individual read/write of
    _listen_process was already lock-protected, so this was never a data
    race - but the two *sequences* themselves were not mutually
    exclusive, meaning e.g. two overlapping stop_listener_process() calls
    could run redundantly). Both paths now hold self._radio_lock for
    their entire prepare sequence (see _claim_radio()'s "DELIBERATE
    DIVERGENCE" comment and connect()'s force branch) - this test proves
    the two critical sections never overlap in wall-clock time, using an
    exclusive-entry counter that would go above 1 if they did.
    """
    transport = _make_transport()
    violations = []
    in_critical_section = {"count": 0}
    counter_lock = threading.Lock()

    @contextmanager
    def _track_critical_section():
        with counter_lock:
            in_critical_section["count"] += 1
            if in_critical_section["count"] > 1:
                violations.append(in_critical_section["count"])
        try:
            time.sleep(0.1)
            yield
        finally:
            with counter_lock:
                in_critical_section["count"] -= 1

    original_stop = transport._stop_listener_process

    def tracked_stop():
        with _track_critical_section():
            return original_stop()

    def tracked_wait(timeout=8):
        with _track_critical_section():
            return True

    transport._stop_listener_process = tracked_stop
    transport._wait_serial_release = tracked_wait

    class _FakePacket:
        id = 1

    class _FakeInterface:
        def sendText(self, **kwargs):
            return _FakePacket()

        def close(self):
            pass

    transport._open_interface = lambda: _FakeInterface()

    def run_connect():
        try:
            transport.connect(
                ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyFAKE"),
                force=True,
                timeout=0.3,
            )
        except TransportError:
            # Task 48 follow-up: connect() now actually opens/closes
            # _open_interface() (stubbed above) instead of polling
            # is_connected() against a listener process this instance
            # never owns - with a fake interface that never raises, this
            # should succeed well within 0.3s, so this except is no
            # longer the expected path (kept defensively: this test's
            # actual point is the critical-section overlap check below,
            # not connect()'s own success/failure).
            pass

    def run_send():
        transport.send_text(
            OutgoingMessage(text="hi", destination_id="^all"), timeout=5.0
        )

    connect_thread = threading.Thread(target=run_connect)
    send_thread = threading.Thread(target=run_send)
    connect_thread.start()
    time.sleep(0.02)
    send_thread.start()
    connect_thread.join(timeout=5.0)
    send_thread.join(timeout=5.0)

    assert not connect_thread.is_alive() and not send_thread.is_alive()
    assert violations == [], (
        "connect(force=True)'s teardown and send_text()'s prepare phase "
        "overlapped - radio_lock is not fully serializing the prepare "
        "sequence, reintroducing the serial-port-contention risk"
    )


# --- Task 48 follow-up: connect()/disconnect() state correctness ----------


def test_connect_reports_state_connected_not_disconnected_after_a_real_probe():
    """Task 48 follow-up review requirement: proving connect() no longer
    raises is not enough - it must also report the RIGHT state
    afterward, not silently fall back to DISCONNECTED via a dead
    is_connected() check against a listener process this instance never
    owns (the second, distinct bug found alongside the polling-loop
    fix). Full path through the real connect() (not a mock of
    connect() itself) with only _open_interface() stubbed - proves
    is_connected()/get_connection_info() correctly derive from the new
    _last_probe_ok state set by _do_connect()'s real body."""
    transport = _make_transport()

    class _FakeInterface:
        def close(self):
            pass

    transport._open_interface = lambda: _FakeInterface()

    info = transport.connect(
        ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyFAKE"),
        timeout=5.0,
    )

    assert info.state == ConnectionState.CONNECTED
    assert transport.is_connected() is True


def test_abandoned_connect_thread_cannot_retroactively_overwrite_probe_state():
    """Task 48 follow-up review requirement: _last_probe_ok must never be
    written from inside the tier-1-abandoned background thread
    (_call_with_timeout()'s documented tier-1/tier-2 gap - a timeout
    releases the caller immediately but does not kill the thread still
    running _do_connect()). Before this fix, a slow-but-eventually-
    successful open() could finish AFTER the caller already received
    TransportError(TIMEOUT) and silently flip _last_probe_ok back to
    True behind the caller's back - a live-caught, real risk, not
    hypothetical. Proven here: a fake interface whose open() sleeps
    longer than the caller's declared timeout but then succeeds -
    connect() must raise TIMEOUT on time, and is_connected() must still
    read False even after waiting long enough for the abandoned thread
    to have actually finished (it's a daemon thread, so the test doesn't
    need to wait for it explicitly - just long enough that if it were
    still writing shared state, this test would catch it)."""
    transport = _make_transport()

    class _SlowThenOkInterface:
        def close(self):
            pass

    def _slow_open():
        time.sleep(0.4)
        return _SlowThenOkInterface()

    transport._open_interface = _slow_open

    with pytest.raises(TransportError) as excinfo:
        transport.connect(
            ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyFAKE"), timeout=0.1
        )
    assert excinfo.value.code == TransportErrorCode.TIMEOUT

    # Give the abandoned background thread time to actually finish its
    # slow-but-successful open()+close() - if _last_probe_ok were still
    # written from inside _do_connect(), this sleep is exactly the
    # window in which it would flip back to True after the fact.
    time.sleep(0.5)

    assert transport.is_connected() is False
    assert transport.get_connection_info().state == ConnectionState.DISCONNECTED


def test_connect_reports_state_disconnected_when_the_probe_fails():
    """Mirror of the success case: a failing probe (device never
    responds - matches StreamInterface.__init__ raising when the real
    handshake fails) must leave is_connected() reporting False, not a
    stale True from a previous successful connect()."""
    transport = _make_transport()

    class _FakeInterface:
        def close(self):
            pass

    transport._open_interface = lambda: _FakeInterface()
    transport.connect(
        ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyFAKE"), timeout=5.0
    )
    assert transport.is_connected() is True  # sanity: really was connected first

    def _raise_open():
        raise TransportError(TransportErrorCode.DEVICE_NOT_FOUND, "radio did not respond")

    transport._open_interface = _raise_open

    with pytest.raises(TransportError):
        transport.connect(
            ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyFAKE"), timeout=5.0
        )

    assert transport.is_connected() is False


def test_disconnect_is_an_explicit_successful_noop_and_clears_probe_state():
    """Task 48 follow-up, explicit decision (not silent): this instance
    never holds a persistent interface between calls, so disconnect()
    has nothing to release - it must still return cleanly (no
    exception) and mark the connection no longer proven, so a stale
    CONNECTED doesn't linger after disconnect()."""
    transport = _make_transport()

    class _FakeInterface:
        def close(self):
            pass

    transport._open_interface = lambda: _FakeInterface()
    transport.connect(
        ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyFAKE"), timeout=5.0
    )
    assert transport.is_connected() is True

    transport.disconnect(timeout=5.0)  # must not raise

    assert transport.is_connected() is False
    assert transport.get_connection_info().state == ConnectionState.DISCONNECTED
