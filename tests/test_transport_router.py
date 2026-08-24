"""Tests for meshsrv/transport_router.py's TransportRouter - delegation,
switch() semantics, and the full-call locking that prevents a send_*/
get_* call from reaching the old transport while a switch to a new one
is in progress (the node only accepts one active link at a time - Task
43/46).
"""
import threading
import time

import pytest

from meshsrv.radio_transport import ConnectionInfo, ConnectionState
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
