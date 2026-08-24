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
            pass  # expected: no run_listener() thread here to ever report is_connected()

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
