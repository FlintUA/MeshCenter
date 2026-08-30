"""Tests for server.py's PAUSED-ceiling fix (Droidian-caught follow-up).

A repeating retry loop (each individual claim/adapter call bounded and
killed on schedule, the overall cycle itself not) can keep re-pausing the
listener indefinitely, with process_listener_autorecovery()'s own
"PAUSED never triggers recovery" rule leaving it stuck with no upper
bound - live-observed on Droidian: two separate 3-6 minute stalls, one
ended only by a manual "Release radio" action, the other by chance after
~3 minutes.

resolve_paused_recovery_status() is the extracted, directly-testable
piece (radio_health_worker() itself just calls it once per 30s poll and
is not exercised here - see its own docstring for why).
"""
import pytest


@pytest.fixture
def _clean_recovery_state(server_module):
    """listener_recovery_state is module-global - reset it around every
    test so one test's progression can't leak into the next."""
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
def logged(_clean_recovery_state, monkeypatch):
    """Captures every log_system_event() call made through server.py's
    module-level reference (resolve_paused_recovery_status() and
    process_listener_autorecovery() both call it by that name)."""
    calls = []
    monkeypatch.setattr(
        _clean_recovery_state, "log_system_event",
        lambda title, level="INFO", details="", source="system": calls.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    return calls


# --- resolve_paused_recovery_status() ---------------------------------


def test_status_unchanged_before_warning_threshold(_clean_recovery_state, logged):
    server_module = _clean_recovery_state
    status, escalated = server_module.resolve_paused_recovery_status("PAUSED", now_ts=1000)

    assert status == "PAUSED"
    assert escalated is False
    assert logged == []


def test_warning_logged_once_at_threshold_not_repeated(_clean_recovery_state, logged):
    server_module = _clean_recovery_state
    T = server_module.LISTENER_PAUSED_WARNING_THRESHOLD_S

    server_module.resolve_paused_recovery_status("PAUSED", now_ts=0)  # starts the timer
    status, escalated = server_module.resolve_paused_recovery_status("PAUSED", now_ts=T)

    assert status == "PAUSED"
    assert escalated is False
    assert len(logged) == 1
    assert logged[0]["title"] == "Listener PAUSED unusually long"
    assert logged[0]["level"] == "WARNING"

    # A later poll still under the escalate threshold must not log again.
    server_module.resolve_paused_recovery_status("PAUSED", now_ts=T + 1)
    assert len(logged) == 1


def test_escalates_to_listener_down_at_threshold(_clean_recovery_state, logged):
    server_module = _clean_recovery_state
    T = server_module.LISTENER_PAUSED_ESCALATE_THRESHOLD_S

    server_module.resolve_paused_recovery_status("PAUSED", now_ts=0)
    status, escalated = server_module.resolve_paused_recovery_status("PAUSED", now_ts=T)

    assert status == "LISTENER_DOWN"
    assert escalated is True


def test_escalation_requires_continuous_pause_not_a_single_sample(_clean_recovery_state, logged):
    """A PAUSED sample that's immediately followed by a non-PAUSED one
    must reset the timer - escalation is about a persisted condition,
    not one unlucky 30s sample."""
    server_module = _clean_recovery_state
    T = server_module.LISTENER_PAUSED_ESCALATE_THRESHOLD_S

    server_module.resolve_paused_recovery_status("PAUSED", now_ts=0)
    server_module.resolve_paused_recovery_status("OK", now_ts=10)

    status, escalated = server_module.resolve_paused_recovery_status("PAUSED", now_ts=10 + T - 1)

    assert status == "PAUSED"
    assert escalated is False


def test_non_paused_status_passes_through_unchanged(_clean_recovery_state, logged):
    server_module = _clean_recovery_state
    for other_status in ("OK", "STARTING", "IDLE", "NO_PACKETS", "LISTENER_DOWN", "RELEASED"):
        status, escalated = server_module.resolve_paused_recovery_status(other_status, now_ts=99999)
        assert status == other_status
        assert escalated is False
    assert logged == []


# --- process_listener_autorecovery()'s escalated-specific wording -----


@pytest.fixture
def _stub_restart_actions(server_module, monkeypatch):
    """The RESTART LISTENER branch calls real stop_listener()/radio_event()
    - stub them so the test exercises only the recovery state machine and
    log wording, not actual subprocess/listener management."""
    monkeypatch.setattr(server_module, "stop_listener", lambda: True)
    monkeypatch.setattr(server_module, "radio_event", lambda *a, **k: None)
    with server_module.state_lock:
        server_module.settings["listener_autorecovery"] = {"enabled": True, "delay": 30}
    return server_module


def _drive_to_limit(server_module, escalated_from_paused):
    """Runs process_listener_autorecovery() through enough calls to
    exhaust LISTENER_RECOVERY_MAX_ATTEMPTS, staying in status="LISTENER_DOWN"
    (with listener_running=False) throughout so every restart attempt is
    reported as still-failed - deterministic, no real listener involved.

    Matches the function's own state machine exactly (delay=30, matching
    _stub_restart_actions' settings): detect -> [wait delay -> trigger
    restart -> wait RESULT_TIMEOUT -> mark failed] x MAX_ATTEMPTS -> wait
    delay once more -> safety-limit check fires."""
    delay = 30
    now_ts = 0.0

    def _call(ts):
        server_module.process_listener_autorecovery(
            status="LISTENER_DOWN", listener_running=False, now_ts=ts,
            escalated_from_paused=escalated_from_paused,
        )

    _call(now_ts)  # initial detection, sets down_since

    for _ in range(server_module.LISTENER_RECOVERY_MAX_ATTEMPTS):
        now_ts += delay
        _call(now_ts)  # triggers a restart attempt
        now_ts += server_module.LISTENER_RECOVERY_RESULT_TIMEOUT
        _call(now_ts)  # marks that attempt failed, resets down_since

    now_ts += delay
    _call(now_ts)  # attempts == MAX_ATTEMPTS -> "limit reached"


def test_limit_reached_message_mentions_paused_when_escalated(_stub_restart_actions, logged):
    server_module = _stub_restart_actions
    _drive_to_limit(server_module, escalated_from_paused=True)

    limit_logs = [entry for entry in logged if entry["title"] == "Automatic recovery limit reached"]
    assert len(limit_logs) == 1
    assert limit_logs[0]["level"] == "ERROR"
    assert "PAUSED persists" in limit_logs[0]["details"]
    assert "manual intervention required" in limit_logs[0]["details"].lower()


def test_limit_reached_message_stays_generic_when_not_escalated(_stub_restart_actions, logged):
    """Regression guard: a genuine LISTENER_DOWN (not escalated from a
    stuck PAUSED state) must keep its original, pre-existing wording -
    this fix must not change behavior for the case it wasn't about."""
    server_module = _stub_restart_actions
    _drive_to_limit(server_module, escalated_from_paused=False)

    limit_logs = [entry for entry in logged if entry["title"] == "Automatic recovery limit reached"]
    assert len(limit_logs) == 1
    assert "PAUSED" not in limit_logs[0]["details"]
    assert limit_logs[0]["details"] == "3 attempts within 30 minutes. Manual action required."


def test_initial_detection_message_mentions_paused_when_escalated(_stub_restart_actions, logged):
    server_module = _stub_restart_actions
    server_module.process_listener_autorecovery(
        status="LISTENER_DOWN", listener_running=False, now_ts=0,
        escalated_from_paused=True,
    )

    detection_logs = [entry for entry in logged if entry["title"] == "Listener failure detected"]
    assert len(detection_logs) == 1
    assert "PAUSED unusually long" in detection_logs[0]["details"]
