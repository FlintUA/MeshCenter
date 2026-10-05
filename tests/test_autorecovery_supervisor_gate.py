"""Review round 4, item 2 (H2-C Phase 2 live round): while listener_
supervisor's own disconnect-recovery/mismatch state machine owns recovery,
the older, blunter process_listener_autorecovery() (a plain "stop then
Popen again" mechanism that predates H2-C) must stand down entirely rather
than race it. Live-caught on dev (2026-10-04): its stop_listener() call
raced _await_identity_before_restart()'s own radio_lock-held --info probe
mid-flight, discarding a MATCH that would otherwise have landed one cycle
earlier.

Also covers compute_radio_health_status()'s new awaiting_identity branch,
which replaces the misleading "LISTENER_DOWN"/"Restart the Meshtastic
listener" messaging with a self-resolving "waiting for identity" status
while the supervisor is in that same state.
"""
import pytest


@pytest.fixture
def _clean_recovery_state(server_module):
    state = server_module.listener_recovery_state
    original = dict(state)
    state.update({
        "down_since": None,
        "attempts": [],
        "restart_pending": False,
        "restart_requested_at": None,
        "limit_logged": False,
        "last_enabled": None,
        "paused_since": None,
        "paused_warning_logged": False,
    })
    yield server_module
    state.clear()
    state.update(original)


@pytest.fixture
def _stub_restart_actions(_clean_recovery_state, monkeypatch):
    server_module = _clean_recovery_state
    monkeypatch.setattr(server_module, "stop_listener", lambda: True)
    monkeypatch.setattr(server_module, "radio_event", lambda *a, **k: None)
    monkeypatch.setattr(server_module, "log_system_event", lambda *a, **k: None)
    with server_module.state_lock:
        server_module.settings["listener_autorecovery"] = {"enabled": True, "delay": 30}
    return server_module


def test_autorecovery_stands_down_while_supervisor_owns_recovery(_stub_restart_actions, monkeypatch):
    server_module = _stub_restart_actions
    monkeypatch.setattr(server_module.listener_supervisor, "owns_recovery", lambda: True)
    stop_calls = []
    monkeypatch.setattr(server_module, "stop_listener", lambda: stop_calls.append(1) or True)

    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=0,
        escalated_from_paused=False,
    )
    # Even well past the point a real restart would normally fire.
    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=9999,
        escalated_from_paused=False,
    )

    assert stop_calls == [], "must never call stop_listener() while the supervisor owns recovery"
    assert server_module.listener_recovery_state["down_since"] is None
    assert server_module.listener_recovery_state["restart_pending"] is False


def test_autorecovery_resumes_normally_once_supervisor_releases_recovery(_stub_restart_actions, monkeypatch):
    """Not a permanent disable - once owns_recovery() goes back to False
    (the supervisor resolved MATCH/mismatch on its own, or simply isn't
    in a recovery state), the older mechanism must behave exactly as
    before."""
    server_module = _stub_restart_actions
    monkeypatch.setattr(server_module.listener_supervisor, "owns_recovery", lambda: False)
    stop_calls = []
    monkeypatch.setattr(server_module, "stop_listener", lambda: stop_calls.append(1) or True)

    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=0,
        escalated_from_paused=False,
    )
    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=30,
        escalated_from_paused=False,
    )

    assert stop_calls == [1], "must restart normally once the supervisor no longer owns recovery"


def test_autorecovery_disabled_by_setting_still_wins_regardless_of_supervisor_state(_stub_restart_actions, monkeypatch):
    """The pre-existing enabled/disabled setting gate is unaffected - this
    is a second, independent reason to stand down, not a replacement."""
    server_module = _stub_restart_actions
    with server_module.state_lock:
        server_module.settings["listener_autorecovery"] = {"enabled": False, "delay": 30}
    monkeypatch.setattr(server_module.listener_supervisor, "owns_recovery", lambda: False)
    stop_calls = []
    monkeypatch.setattr(server_module, "stop_listener", lambda: stop_calls.append(1) or True)

    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=0,
        escalated_from_paused=False,
    )
    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=9999,
        escalated_from_paused=False,
    )

    assert stop_calls == []


# --- compute_radio_health_status()'s awaiting_identity branch -----------


def test_awaiting_identity_status_when_supervisor_owns_recovery(server_module):
    status, level, reason, recommendation = server_module.compute_radio_health_status(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={},
        awaiting_identity=True,
    )
    assert status == "AWAITING_IDENTITY"
    assert level == "WARNING"
    assert "identity" in reason.lower() or "radio" in reason.lower()


def test_listener_down_status_unchanged_when_not_awaiting_identity(server_module):
    """Regression guard: the ordinary case (listener crashed, supervisor
    NOT in disconnect-recovery) must keep reporting LISTENER_DOWN exactly
    as before - this fix only adds a new branch, it doesn't touch the
    existing one."""
    status, level, reason, recommendation = server_module.compute_radio_health_status(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={},
        awaiting_identity=False,
    )
    assert status == "LISTENER_DOWN"
    assert level == "ERROR"


def test_awaiting_identity_defaults_to_false(server_module):
    """Every other existing caller/test of compute_radio_health_status()
    omits awaiting_identity entirely - must default to the pre-existing
    LISTENER_DOWN behavior, not silently change it."""
    status, level, reason, recommendation = server_module.compute_radio_health_status(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={},
    )
    assert status == "LISTENER_DOWN"
