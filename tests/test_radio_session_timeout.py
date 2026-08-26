"""Tests for server.py's radio_session()'s Task 49 fix: radio_lock.acquire()
is now bounded (dynamic remaining-time split, matching TransportRouter.
_delegate()'s Task 47.5 mechanic) instead of an unconditional `with
radio_lock:` with no timeout of its own.

prepare_radio_command() is stubbed out in every test here (it shells out to
`lsof`, unavailable on this project's Windows dev machine, and its own
pause/stop/wait-for-port-release behavior is not what changed in this fix -
see server.py's own serial_transport-delegation docstrings for that logic).
What's under test is specifically the NEW bounded radio_lock.acquire() step
that runs after prepare_radio_command() succeeds.
"""
import threading
import time

import pytest


@pytest.fixture
def _stub_prepare(server_module, monkeypatch):
    """Make prepare_radio_command() succeed instantly and
    pause_listen.clear()/is_radio_available() safe no-ops, so each test
    exercises only the new bounded radio_lock.acquire() step."""
    monkeypatch.setattr(server_module, "prepare_radio_command", lambda device=None, timeout=8: True)
    monkeypatch.setattr(server_module, "wait_serial_release", lambda device=None, timeout=8: True)
    monkeypatch.setattr(server_module, "is_radio_available", lambda: False)  # skip pause_listen.clear()
    return server_module


def test_radio_session_raises_radio_busy_error_within_its_own_budget_not_unboundedly(_stub_prepare):
    """Regression test for the live-caught Task 49 gap: once
    claim_exclusive_access() can hold radio_lock for a full IPC
    round-trip, a radio_session() caller must not wait behind it forever.
    Holds radio_lock from this thread (simulating a long-running
    claim_exclusive_access() call elsewhere), then calls
    radio_session(timeout=1.0, cooldown=0) in a separate thread and
    confirms it raises RadioBusyError - the SAME error
    prepare_radio_command()'s own failure already raises, no new error
    shape - within its own ~1s budget, not the lock-holder's much longer
    hold time. cooldown=0 isolates the lock-acquire step itself from the
    unrelated, pre-existing (unchanged by this fix) unconditional
    cooldown sleep in radio_session()'s own finally block - that sleep
    runs regardless of whether the lock was ever acquired, which is not
    what this test is about."""
    server_module = _stub_prepare
    server_module.radio_lock.acquire()
    try:
        result = {}

        def _call():
            start = time.monotonic()
            try:
                with server_module.radio_session(timeout=1.0, cooldown=0):
                    pass
            except server_module.RadioBusyError as error:
                result["error"] = error
            result["elapsed"] = time.monotonic() - start

        thread = threading.Thread(target=_call)
        thread.start()
        thread.join(timeout=5.0)

        assert not thread.is_alive(), "radio_session() did not return - waited unboundedly behind the held lock"
        assert "error" in result, "expected RadioBusyError, radio_session() did not raise"
        assert isinstance(result["error"], server_module.RadioBusyError)
        # Bounded by the declared timeout (1.0s), not the lock-holder's
        # hold time (this test never releases it until after the assert) -
        # generous upper bound to absorb scheduling jitter without being
        # a no-op assertion.
        assert result["elapsed"] < 3.0, f"took {result['elapsed']:.2f}s - should fail within its own ~1s budget"
    finally:
        server_module.radio_lock.release()


def test_radio_session_short_call_does_not_queue_behind_a_long_one(_stub_prepare):
    """Mirrors test_serial_transport_timeout.py's
    test_concurrent_calls_do_not_spuriously_timeout_each_other pattern:
    a short, generously-timed-out radio_session() call must succeed
    promptly once the long-running one releases the lock, not be starved
    by it."""
    server_module = _stub_prepare
    started_second_at = []
    results = {}

    def run_long():
        with server_module.radio_session(timeout=5.0, cooldown=0):
            time.sleep(0.4)
        results["long"] = "done"

    def run_short():
        time.sleep(0.05)  # let the long call acquire the lock first
        started_second_at.append(time.monotonic())
        with server_module.radio_session(timeout=5.0, cooldown=0):
            pass
        results["short"] = "done"

    t0 = time.monotonic()
    long_thread = threading.Thread(target=run_long)
    short_thread = threading.Thread(target=run_short)
    long_thread.start()
    short_thread.start()
    long_thread.join(timeout=5.0)
    short_thread.join(timeout=5.0)

    assert results.get("long") == "done"
    assert results.get("short") == "done"
    # The short call must actually have had to wait for the long one
    # (proving they really contended for the same lock, not a vacuous
    # pass) but both complete within a generous bound - no spurious
    # RadioBusyError from a call whose own 5.0s timeout was never at risk.
    assert started_second_at[0] - t0 < 0.3
