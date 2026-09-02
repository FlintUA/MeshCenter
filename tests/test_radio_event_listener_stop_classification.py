"""Tests for server.py's radio_event() "listener_stop" classification -
P1-B stabilization follow-up.

Root cause (live-observed on dev after PR #166: an ERROR "Listener
stopped ... exited unexpectedly" firing on almost every routine radio
command): meshsrv/serial_port_supervisor.py's run_listener() knows,
synchronously at the exact moment of a stop, whether it was intentional
(pause_listen.is_set() at that instant) - but that value used to only
cross the on_lifecycle_event("listener_stop") boundary as an event name,
not a value. radio_event() then re-read pause_listen.is_set() itself,
asynchronously, at whatever later moment its own callback actually ran -
and a different radio_session()/prepare_radio_command() caller could
have legitimately changed the same shared pause_listen Event in that
window, misclassifying a routine stop as unexpected (or vice versa).

The fix threads the already-known value through as an `intentional`
parameter instead of re-deriving it later - these tests exercise
radio_event() directly with that parameter, using the same
server_module + log_system_event-monkeypatch pattern as
tests/test_listener_paused_recovery.py's own `logged` fixture (not
modified here, just followed as a template).
"""
import pytest


@pytest.fixture
def radio_env(server_module):
    """radio_health/its history list and pause_listen are module-global
    state - reset around every test so one test's transition can't leak
    into the next. listener_running=True going in, since radio_event()'s
    listener_stop branch only logs anything when was_running is True."""
    original_running = server_module.radio_health.get("listener_running")
    original_history = list(server_module.radio_health.get("history", []))
    original_pause_listen_set = server_module.pause_listen.is_set()

    server_module.radio_health["listener_running"] = True

    yield server_module

    server_module.radio_health["listener_running"] = original_running
    server_module.radio_health["history"] = original_history
    if original_pause_listen_set:
        server_module.pause_listen.set()
    else:
        server_module.pause_listen.clear()


@pytest.fixture
def logged(radio_env, monkeypatch):
    """Captures every log_system_event() call - _radio_history_locked()
    (radio_event()'s own logging helper) calls it with source="radio"."""
    calls = []
    monkeypatch.setattr(
        radio_env, "log_system_event",
        lambda title, level="INFO", details="", source="system": calls.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    return calls


def test_intentional_stop_logs_listener_paused_info(radio_env, logged):
    radio_env.radio_event("listener_stop", intentional=True)

    assert len(logged) == 1
    assert logged[0]["title"] == "Listener paused"
    assert logged[0]["level"] == "INFO"
    assert logged[0]["details"] == "Listener stopped temporarily for a radio command"


def test_unintentional_stop_logs_listener_stopped_error(radio_env, logged):
    radio_env.radio_event("listener_stop", intentional=False)

    assert len(logged) == 1
    assert logged[0]["title"] == "Listener stopped"
    assert logged[0]["level"] == "ERROR"
    assert logged[0]["details"] == "Meshtastic listener exited unexpectedly"


def test_classification_uses_the_passed_value_not_a_fresh_reread_of_pause_listen(radio_env, logged):
    """The actual bug this fixes, proven directly: set pause_listen to
    the OPPOSITE of what `intentional` says, and confirm the log follows
    the passed-in value - not pause_listen.is_set(). If radio_event()
    were still re-deriving the classification from a fresh read (the
    pre-fix behavior), this test would get the wrong log entry."""
    # pause_listen is SET (a stale re-read would say "intentional") but
    # the value captured at the actual transition says otherwise - the
    # exact race window this fix closes.
    radio_env.pause_listen.set()
    radio_env.radio_event("listener_stop", intentional=False)

    assert len(logged) == 1
    assert logged[0]["title"] == "Listener stopped"
    assert logged[0]["level"] == "ERROR"

    logged.clear()
    radio_env.radio_health["listener_running"] = True

    # And the reverse: pause_listen is CLEAR (a stale re-read would say
    # "unexpected") but the captured value says it was intentional.
    radio_env.pause_listen.clear()
    radio_env.radio_event("listener_stop", intentional=True)

    assert len(logged) == 1
    assert logged[0]["title"] == "Listener paused"
    assert logged[0]["level"] == "INFO"


def test_stop_while_not_running_logs_nothing(radio_env, logged):
    """was_running gates the log entirely (unchanged pre-existing
    behavior) - a stop event with listener_running already False must
    not produce a spurious log line either way."""
    radio_env.radio_health["listener_running"] = False

    radio_env.radio_event("listener_stop", intentional=True)
    radio_env.radio_event("listener_stop", intentional=False)

    assert logged == []
