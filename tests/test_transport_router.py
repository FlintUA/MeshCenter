"""Tests for meshsrv/transport_router.py's TransportRouter - delegation,
switch() semantics, and the full-call locking that prevents a send_*/
get_* call from reaching the old transport while a switch to a new one
is in progress (the node only accepts one active link at a time - Task
43/46).
"""
import threading
import time

import pytest

from meshsrv.radio_transport import (
    ConnectionInfo,
    ConnectionState,
    TransportError,
    TransportErrorCode,
)
from meshsrv.transport_router import TransportRouter


class _FakeTransport:
    def __init__(self, name):
        self.name = name
        self.send_text_calls = []

    def send_text(self, message, timeout=15.0):
        self.send_text_calls.append(message)
        return f"sent-by-{self.name}"

    def get_connection_info(self):
        return ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=None, node_id=self.name)


def test_delegates_to_the_active_transport():
    a = _FakeTransport("a")
    router = TransportRouter(a)

    result = router.send_text("hello")

    assert result == "sent-by-a"
    assert a.send_text_calls == ["hello"]


def test_switch_success_changes_the_active_transport():
    a = _FakeTransport("a")
    b = _FakeTransport("b")
    router = TransportRouter(a)

    old = router.switch(lambda: b)

    assert old is a
    assert router.send_text("hi") == "sent-by-b"


def test_switch_failure_leaves_active_unchanged():
    a = _FakeTransport("a")
    router = TransportRouter(a)

    def _fail():
        raise RuntimeError("simulated connect failure")

    with pytest.raises(RuntimeError):
        router.switch(_fail)

    # self._active must still be `a` - switch() only reassigns after
    # connect_new() succeeds.
    assert router.send_text("still here") == "sent-by-a"


def test_concurrent_call_blocks_until_switch_completes_not_a_race():
    """Regression test for the review finding: locking only the read of
    self._active would let a send_*/get_* call reach the OLD transport
    while connect_new() is still busy connecting the NEW one - a race
    for the same physical radio at the firmware level, not just Python.
    Full-call locking means a concurrent call must wait for the entire
    switch (including connect_new()'s duration) to finish, landing on
    whichever transport is active *after* the switch - it must never
    observe a state where "the old one is definitely gone but the new
    one isn't active yet".
    """
    a = _FakeTransport("a")
    b = _FakeTransport("b")
    router = TransportRouter(a)

    switch_in_progress = threading.Event()
    release_connect = threading.Event()

    def _slow_connect_new():
        switch_in_progress.set()
        release_connect.wait(timeout=5)
        return b

    results = {}

    def _do_switch():
        router.switch(_slow_connect_new)

    switch_thread = threading.Thread(target=_do_switch)
    switch_thread.start()
    assert switch_in_progress.wait(timeout=5), "switch() never started connect_new()"

    def _do_send():
        # Must block here until the switch finishes - if it read `a` and
        # returned immediately, that would prove the lock isn't covering
        # the whole switch.
        results["send"] = router.send_text("during switch")

    send_thread = threading.Thread(target=_do_send)
    t0 = time.monotonic()
    send_thread.start()
    time.sleep(0.1)  # give it a chance to (wrongly) return early if unlocked
    assert "send" not in results, "send_text() returned before the switch finished - not actually locked"

    release_connect.set()
    switch_thread.join(timeout=5)
    send_thread.join(timeout=5)
    elapsed = time.monotonic() - t0

    assert results["send"] == "sent-by-b", "call landed on the transport active after the switch, as required"
    assert elapsed >= 0.1


def test_send_text_fails_fast_with_busy_within_its_own_timeout_not_the_lock_holders():
    """Regression test for the Task 47.5 review finding: the lock-acquire
    itself used to be unconditional, so a caller's own `timeout` only
    ever bounded the operation *after* the lock was free - a send_text
    landing during a long switch waited (switch duration + its own
    timeout), not its own timeout alone. Proven here by holding the lock
    far longer (10s) than the caller's declared budget (1s) and asserting
    the caller still gets a bounded, fast TransportError(BUSY) - not by
    literally reproducing a 135s switch, which would make this test slow
    without testing anything the short version doesn't already prove."""
    a = _FakeTransport("a")
    router = TransportRouter(a)

    lock_released = threading.Event()

    def _hold_lock():
        router._lock.acquire()
        lock_released.wait(timeout=10)
        router._lock.release()

    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    time.sleep(0.1)  # let the holder actually acquire the lock first

    start = time.monotonic()
    with pytest.raises(TransportError) as excinfo:
        router.send_text("hello", timeout=1.0)
    elapsed = time.monotonic() - start

    assert excinfo.value.code == TransportErrorCode.BUSY
    assert elapsed < 2.0, f"took {elapsed:.2f}s - should fail within its own ~1s budget, not the holder's 10s"

    lock_released.set()
    holder.join(timeout=2)


def test_get_connection_info_returns_synthetic_response_when_lock_busy_not_an_exception():
    """get_connection_info() has no `timeout` parameter and must not
    raise per the ABC contract - on a busy lock it must return a fast,
    synthetic ConnectionInfo instead."""
    a = _FakeTransport("a")
    router = TransportRouter(a)

    router._lock.acquire()
    try:
        start = time.monotonic()
        info = router.get_connection_info()
        elapsed = time.monotonic() - start

        assert info.state == ConnectionState.CONNECTING
        assert info.last_error.code == TransportErrorCode.BUSY
        assert elapsed < router._INFO_LOCK_TIMEOUT_S + 1.0
    finally:
        router._lock.release()


def test_is_connected_returns_false_when_lock_busy_not_an_exception():
    a = _FakeTransport("a")
    router = TransportRouter(a)

    router._lock.acquire()
    try:
        start = time.monotonic()
        result = router.is_connected()
        elapsed = time.monotonic() - start

        assert result is False
        assert elapsed < router._INFO_LOCK_TIMEOUT_S + 1.0
    finally:
        router._lock.release()
