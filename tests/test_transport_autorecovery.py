"""Tests for server.py's process_transport_autorecovery() - the TCP/
Bluetooth counterpart to process_listener_autorecovery() (see
tests/test_listener_paused_recovery.py for that one). Live motivation:
nothing in this codebase automatically reconnected a TCP/Bluetooth radio
once DISCONNECTED/ERROR - confirmed live on pixel-111 across two real
reboots (the boot-race retry in verify_radio_identity() covers "never
connected in the first place" separately; this covers both that case and
"was connected, then dropped", identically, by design - see the
function's own docstring).
"""
import threading
import time

import pytest

from meshsrv.radio_transport import TransportError, TransportErrorCode


@pytest.fixture
def _clean_recovery_state(server_module):
    state = server_module.transport_recovery_state
    original = dict(state)
    state.update({
        "consecutive_bad_cycles": 0,
        "attempts": [],
        "in_progress": False,
        "last_enabled": None,
        "limit_logged": False,
    })
    with server_module.state_lock:
        server_module.settings["tcp_ble_autorecovery"] = {"enabled": True}
    yield server_module
    state.clear()
    state.update(original)


@pytest.fixture
def logged(_clean_recovery_state, monkeypatch):
    calls = []
    monkeypatch.setattr(
        _clean_recovery_state, "log_system_event",
        lambda title, level="INFO", details="", source="system": calls.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    return calls


@pytest.fixture
def sync_thread(_clean_recovery_state, monkeypatch):
    """Replaces threading.Thread with a fake that runs its target
    synchronously on .start() instead of a real background thread -
    makes the trigger-logic tests fully deterministic (no sleeps, no
    real concurrency) while still exercising the exact same code path
    process_transport_autorecovery() actually calls."""
    server_module = _clean_recovery_state

    class _SyncThread:
        def __init__(self, target=None, daemon=None, **kwargs):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(server_module.threading, "Thread", _SyncThread)
    return server_module


def _run(server_module, status="DISCONNECTED", active_transport="tcp", now_ts=0.0):
    server_module.process_transport_autorecovery(
        status=status, active_transport=active_transport, now_ts=now_ts,
    )


# --- gating: disabled / serial / healthy status ------------------------


def test_disabled_never_triggers(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    with server_module.state_lock:
        server_module.settings["tcp_ble_autorecovery"] = {"enabled": False}

    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))

    for now_ts in (0.0, 30.0, 60.0, 90.0):
        _run(server_module, now_ts=now_ts)

    assert calls == []
    triggered = [e for e in logged if e["title"] == "TCP/Bluetooth auto-reconnect triggered"]
    assert triggered == []


def test_serial_active_transport_never_triggers(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))

    for now_ts in (0.0, 30.0, 60.0, 90.0):
        _run(server_module, active_transport="serial", now_ts=now_ts)

    assert calls == []


def test_connected_status_never_triggers(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))

    for status in ("CONNECTED", "CONNECTING"):
        for now_ts in (0.0, 30.0, 60.0):
            _run(server_module, status=status, now_ts=now_ts)

    assert calls == []


# --- consecutive-cycle threshold ----------------------------------------


def test_single_bad_cycle_does_not_trigger(sync_thread, logged, monkeypatch):
    """A one-off blip must not fire anything - matches
    TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES's own purpose."""
    server_module = sync_thread
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))

    _run(server_module, now_ts=0.0)

    assert calls == []
    assert server_module.transport_recovery_state["consecutive_bad_cycles"] == 1


def test_consecutive_threshold_triggers_exactly_at_the_configured_count(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))
    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES

    for i in range(threshold - 1):
        _run(server_module, now_ts=float(i * 30))
        assert calls == [], f"must not trigger before {threshold} consecutive cycles (cycle {i + 1})"

    _run(server_module, now_ts=float((threshold - 1) * 30))
    assert len(calls) == 1
    assert calls[0]["timeout"] == server_module.TRANSPORT_RECONNECT_TIMEOUT_S


def test_recovering_to_connected_mid_sequence_resets_the_counter(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))

    _run(server_module, status="DISCONNECTED", now_ts=0.0)
    _run(server_module, status="CONNECTED", now_ts=30.0)  # recovers on its own
    _run(server_module, status="DISCONNECTED", now_ts=60.0)  # only 1 consecutive again

    assert calls == []
    assert server_module.transport_recovery_state["consecutive_bad_cycles"] == 1


# --- in_progress guard ---------------------------------------------------


def test_in_progress_blocks_a_second_trigger(sync_thread, logged, monkeypatch):
    """A previously-triggered attempt that hasn't finished yet (in_progress
    still True) must not spawn a second, overlapping reconnect()."""
    server_module = sync_thread
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))

    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES
    for i in range(threshold):
        _run(server_module, now_ts=float(i * 30))
    assert len(calls) == 1

    # Simulate the spawned thread not having finished yet (sync_thread
    # fixture actually runs it synchronously and the fake reconnect()
    # above returns instantly, so in_progress is already False again by
    # this point - force it back to True to exercise the guard itself).
    server_module.transport_recovery_state["in_progress"] = True
    for i in range(threshold, threshold + threshold):
        _run(server_module, now_ts=float(i * 30))

    assert len(calls) == 1, "must not trigger again while in_progress is still True"


def test_in_progress_cleared_after_a_successful_reconnect(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: None)

    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES
    for i in range(threshold):
        _run(server_module, now_ts=float(i * 30))

    assert server_module.transport_recovery_state["in_progress"] is False
    success_logs = [e for e in logged if e["title"] == "TCP/Bluetooth auto-reconnect succeeded"]
    assert len(success_logs) == 1


def test_in_progress_cleared_after_a_failed_reconnect(sync_thread, logged, monkeypatch):
    server_module = sync_thread

    def _fail(**kwargs):
        raise TransportError(TransportErrorCode.CONNECT_FAILED, "simulated failure")

    monkeypatch.setattr(server_module.transport_router, "reconnect", _fail)

    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES
    for i in range(threshold):
        _run(server_module, now_ts=float(i * 30))

    assert server_module.transport_recovery_state["in_progress"] is False
    failed_logs = [e for e in logged if e["title"] == "TCP/Bluetooth auto-reconnect failed"]
    assert len(failed_logs) == 1
    assert "simulated failure" in failed_logs[0]["details"]


# --- attempt cap (mirrors LISTENER_RECOVERY_MAX_ATTEMPTS's own shape) ---


def test_attempt_cap_stops_after_max_attempts_within_the_window(sync_thread, logged, monkeypatch):
    server_module = sync_thread

    def _fail(**kwargs):
        raise TransportError(TransportErrorCode.CONNECT_FAILED, "still down")

    monkeypatch.setattr(server_module.transport_router, "reconnect", _fail)

    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES
    max_attempts = server_module.TRANSPORT_RECOVERY_MAX_ATTEMPTS
    now_ts = 0.0
    step = 30.0

    triggered_count = 0
    for _ in range(max_attempts):
        for _ in range(threshold):
            _run(server_module, now_ts=now_ts)
            now_ts += step
        triggered_count += 1

    assert len(server_module.transport_recovery_state["attempts"]) == max_attempts

    # One more full consecutive-cycle sequence, still within the window -
    # must be refused, not silently retried a 4th time.
    for _ in range(threshold):
        _run(server_module, now_ts=now_ts)
        now_ts += step

    failed_logs = [e for e in logged if e["title"] == "TCP/Bluetooth auto-reconnect failed"]
    assert len(failed_logs) == max_attempts, "must not attempt a 4th reconnect within the window"

    limit_logs = [e for e in logged if e["title"] == "TCP/Bluetooth auto-reconnect limit reached"]
    assert len(limit_logs) == 1


def test_attempt_cap_message_logged_only_once(sync_thread, logged, monkeypatch):
    server_module = sync_thread

    def _fail(**kwargs):
        raise TransportError(TransportErrorCode.CONNECT_FAILED, "still down")

    monkeypatch.setattr(server_module.transport_router, "reconnect", _fail)

    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES
    max_attempts = server_module.TRANSPORT_RECOVERY_MAX_ATTEMPTS
    now_ts = 0.0
    step = 30.0

    for _ in range(max_attempts):
        for _ in range(threshold):
            _run(server_module, now_ts=now_ts)
            now_ts += step

    for _ in range(3):
        for _ in range(threshold):
            _run(server_module, now_ts=now_ts)
            now_ts += step

    limit_logs = [e for e in logged if e["title"] == "TCP/Bluetooth auto-reconnect limit reached"]
    assert len(limit_logs) == 1, "must log the limit-reached warning only once, not every cycle"


# --- enable/disable transition logging (mirrors serial's own pattern) --


def test_enabled_transition_logs_once(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: None)

    _run(server_module, status="CONNECTED", now_ts=0.0)  # first call establishes last_enabled
    _run(server_module, status="CONNECTED", now_ts=30.0)  # unchanged, no new log

    enabled_logs = [e for e in logged if e["title"] == "TCP/Bluetooth Auto-Reconnect enabled"]
    assert len(enabled_logs) == 1


def test_disabling_mid_session_logs_and_resets_counter(sync_thread, logged, monkeypatch):
    server_module = sync_thread
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: None)

    _run(server_module, status="DISCONNECTED", now_ts=0.0)
    assert server_module.transport_recovery_state["consecutive_bad_cycles"] == 1

    with server_module.state_lock:
        server_module.settings["tcp_ble_autorecovery"] = {"enabled": False}
    _run(server_module, status="DISCONNECTED", now_ts=30.0)

    assert server_module.transport_recovery_state["consecutive_bad_cycles"] == 0
    disabled_logs = [e for e in logged if e["title"] == "TCP/Bluetooth Auto-Reconnect disabled"]
    assert len(disabled_logs) == 1


# --- never blocks the caller (real threading, not the sync_thread fixture) --


def test_process_transport_autorecovery_never_blocks_the_caller(_clean_recovery_state, monkeypatch):
    """The actual concurrency requirement: even if transport_router.reconnect()
    itself takes a long time, process_transport_autorecovery() must return
    almost immediately - the real reconnect() call happens on its own
    background thread. Uses REAL threading.Thread (not the sync_thread
    fixture) specifically to prove this."""
    server_module = _clean_recovery_state
    release = threading.Event()

    def _slow_reconnect(**kwargs):
        release.wait(timeout=5.0)

    monkeypatch.setattr(server_module.transport_router, "reconnect", _slow_reconnect)

    threshold = server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES
    for i in range(threshold - 1):
        _run(server_module, now_ts=float(i * 30))

    started = time.monotonic()
    _run(server_module, now_ts=float((threshold - 1) * 30))
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, (
        f"process_transport_autorecovery() took {elapsed:.2f}s - the reconnect() call "
        "must run on a background thread, never block the caller"
    )

    release.set()  # let the background thread finish so it doesn't leak into other tests
    time.sleep(0.2)
