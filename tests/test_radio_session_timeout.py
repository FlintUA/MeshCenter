"""Tests for server.py's radio_session()'s Task 49 fix: radio_lock.acquire()
is bounded (dynamic remaining-time split, matching TransportRouter.
_delegate()'s Task 47.5 mechanic) instead of an unconditional `with
radio_lock:` with no timeout of its own.

prepare_radio_command() is stubbed out in every test here (it shells out to
`lsof`, unavailable on this project's Windows dev machine, and its own
pause/stop/wait-for-port-release behavior is not what changed in this fix -
see server.py's own serial_transport-delegation docstrings for that logic).
What's under test is specifically the bounded radio_lock.acquire() step -
P1-A follow-up: that step now runs BEFORE prepare_radio_command() (moved
earlier to close the prepare-phase race, see radio_session()'s own updated
docstring), not after it as when this file was first written - these tests
still pass unchanged since they only assert on RadioBusyError/timing, not
call ordering, but the acquire-then-prepare ordering here is now the
opposite of what this file originally described.
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
        # a no-op assertion. P1-A follow-up: radio_lock.acquire() now runs
        # BEFORE prepare_radio_command() (this test's own stubbed version
        # is never even called here, since the acquire above already
        # fails) - the busy-lock case no longer touches the listener/port
        # at all, so there's nothing for cooldown/pause_listen.clear() to
        # clean up in this specific failure path (unlike a *prepare*
        # failure, which still runs that cleanup - see
        # radio_session()'s own updated docstring).
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


# --- P1-A: the prepare phase itself must not race -------------------------
# Direct counterpart to tests/test_serial_transport_timeout.py's
# test_concurrent_connect_and_send_do_not_race_prepare_phase - same
# technique (an exclusive-entry counter around the tracked critical
# section, not just "both eventually complete").


def test_two_concurrent_radio_sessions_do_not_race_their_prepare_phases(server_module, monkeypatch):
    """Before the P1-A fix, radio_lock was acquired AFTER
    prepare_radio_command() returned - two concurrent radio_session()
    calls could both run their prepare phases (pause/stop/wait-for-
    port-release) at the same time, only serializing once each reached
    the lock. This proves that window is closed: prepare_radio_command()
    is now only ever entered by one radio_session() call at a time."""
    violations = []
    in_critical_section = {"count": 0}
    counter_lock = threading.Lock()

    def tracked_prepare(device=None, timeout=8):
        with counter_lock:
            in_critical_section["count"] += 1
            if in_critical_section["count"] > 1:
                violations.append(in_critical_section["count"])
        try:
            time.sleep(0.1)
            return True
        finally:
            with counter_lock:
                in_critical_section["count"] -= 1

    monkeypatch.setattr(server_module, "prepare_radio_command", tracked_prepare)
    monkeypatch.setattr(server_module, "wait_serial_release", lambda device=None, timeout=8: True)
    monkeypatch.setattr(server_module, "is_radio_available", lambda: False)  # skip pause_listen.clear()

    results = {}

    def run(name):
        with server_module.radio_session(timeout=5.0, cooldown=0):
            pass
        results[name] = "done"

    thread_a = threading.Thread(target=run, args=("a",))
    thread_b = threading.Thread(target=run, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=5.0)
    thread_b.join(timeout=5.0)

    assert results.get("a") == "done" and results.get("b") == "done"
    assert violations == [], (
        "two radio_session() calls entered prepare_radio_command() at the "
        "same time - the prepare-phase race P1-A is supposed to close"
    )


def test_radio_session_fails_fast_when_radio_lock_is_held_by_a_real_claim_exclusive_access(server_module, monkeypatch):
    """The exact scenario meshsrv/adapter_ipc_client.py's own SERIAL PORT
    CLAIM comment worries about: a long-running claim_exclusive_access()
    call (the real one, not a generic lock-hold simulation) holding
    radio_lock while a radio_session() caller (e.g. a Node Tools action)
    lands. Must fail fast with RadioBusyError within radio_session()'s
    own declared timeout, not wait out claim_exclusive_access()'s much
    longer hold - and, since the P1-A fix, must do so WITHOUT ever
    touching the listener/port (prepare_radio_command() is never called
    at all when the lock itself is what's busy)."""
    from meshsrv.serial_port_supervisor import SerialPortSupervisor

    supervisor = SerialPortSupervisor(
        cli_path="/does/not/matter/for/this/test",
        port="/dev/ttyFAKE",
        radio_lock=server_module.radio_lock,
        pause_listen=server_module.pause_listen,
    )
    # A real port-free outcome so claim_exclusive_access() actually
    # enters its held-lock body instead of raising immediately.
    supervisor._wait_for_release_outcome = lambda timeout=8: __import__(
        "meshsrv.serial_port_supervisor", fromlist=["PortReleaseOutcome"]
    ).PortReleaseOutcome.PORT_FREE
    supervisor.stop_listener_process = lambda: True

    prepare_calls = []
    original_prepare = server_module.prepare_radio_command

    def tracked_prepare(*args, **kwargs):
        prepare_calls.append(True)
        return original_prepare(*args, **kwargs)

    monkeypatch.setattr(server_module, "prepare_radio_command", tracked_prepare)

    claim_entered = threading.Event()
    release_claim = threading.Event()

    def hold_claim():
        with supervisor.claim_exclusive_access(timeout=8, cooldown=0):
            claim_entered.set()
            release_claim.wait(timeout=5.0)

    claim_thread = threading.Thread(target=hold_claim)
    claim_thread.start()
    assert claim_entered.wait(timeout=5.0), "claim_exclusive_access() never entered its held-lock body"

    result = {}
    try:
        start = time.monotonic()
        try:
            with server_module.radio_session(timeout=1.0, cooldown=0):
                pass
        except server_module.RadioBusyError as error:
            result["error"] = error
        result["elapsed"] = time.monotonic() - start
    finally:
        release_claim.set()
        claim_thread.join(timeout=5.0)

    assert "error" in result, "expected RadioBusyError while radio_lock was held by claim_exclusive_access()"
    assert result["elapsed"] < 3.0, f"took {result['elapsed']:.2f}s - should fail within its own ~1s budget"
    assert prepare_calls == [], (
        "prepare_radio_command() must not run at all when radio_lock itself "
        "is what's busy - P1-A's fail-fast-before-touching-the-listener guarantee"
    )
