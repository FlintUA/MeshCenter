"""Tests for system_log.py (F4.1 PR 2).

log_system_event() is called from everywhere in the codebase - request
handlers, background workers, error paths - with no try/except at most call
sites. Before this fix, a write failure (a full disk, a permission issue)
raised straight out of it uncaught: on a full disk, every single log call
anywhere in the app would become an unhandled exception. This hardens it to
never raise, while still surfacing the failure (once per failure streak,
not spammed on every subsequent call) rather than going silent forever.
"""

import pytest


@pytest.fixture
def system_log(server_module, tmp_path, monkeypatch):
    """system_log.py does `from config import DATA_DIR` at module level, so
    it needs the server_module fixture's synthetic config.py on sys.path
    before it can be imported at all - imported lazily here (not at this
    file's top level, which would run during collection, before any
    fixture) rather than via server.py itself, matching this module's own
    "no server.py import needed" shape (it's a standalone module).

    Points SYSTEM_LOG_FILE at a fresh per-test file and resets the
    failure-streak flag so tests can't leak state into each other."""
    import system_log as module

    monkeypatch.setattr(module, "SYSTEM_LOG_FILE", str(tmp_path / "system_events.jsonl"))
    monkeypatch.setattr(module, "_log_write_failing", False, raising=False)
    return module


def test_log_system_event_writes_and_returns_the_event(system_log):
    event = system_log.log_system_event("Test title", level="OK", details="hello", source="test")
    assert event["event"] == "Test title"
    assert event["level"] == "OK"

    events = system_log.get_system_events()
    assert len(events) == 1
    assert events[0]["event"] == "Test title"


def test_log_system_event_never_raises_when_the_append_fails(system_log, monkeypatch):
    def _broken_open(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(system_log, "open", _broken_open, raising=False)

    event = system_log.log_system_event("Will fail to persist", level="ERROR")
    assert event["event"] == "Will fail to persist"  # still returns the event, just didn't persist it


def test_write_failure_is_logged_to_stderr_only_once_per_streak(system_log, monkeypatch, capsys):
    def _broken_open(*args, **kwargs):
        raise OSError("simulated disk full")

    monkeypatch.setattr(system_log, "open", _broken_open, raising=False)

    system_log.log_system_event("fail 1")
    system_log.log_system_event("fail 2")
    system_log.log_system_event("fail 3")

    captured = capsys.readouterr()
    # Exactly one error report across three consecutive failures - not
    # spammed once per call.
    assert captured.err.count("simulated disk full") + captured.out.count("simulated disk full") == 1


def test_failure_streak_resets_after_a_successful_write(system_log, monkeypatch, capsys):
    real_open = open
    state = {"fail": True}

    def _toggle_open(path, *args, **kwargs):
        if state["fail"] and str(path) == system_log.SYSTEM_LOG_FILE:
            raise OSError("simulated disk full")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(system_log, "open", _toggle_open, raising=False)

    system_log.log_system_event("fail 1")
    system_log.log_system_event("fail 2")
    capsys.readouterr()  # drain the first streak's single report

    state["fail"] = False
    system_log.log_system_event("succeeds")

    state["fail"] = True
    system_log.log_system_event("fail again - new streak")

    captured = capsys.readouterr()
    assert captured.err.count("simulated disk full") + captured.out.count("simulated disk full") == 1


def test_trim_failure_does_not_raise(system_log, monkeypatch):
    # Write enough events to trigger a trim, then make the trim's own
    # write fail - the ORIGINAL append (already flushed/fsynced) must not
    # be treated as a failure just because the trim afterward couldn't run.
    for i in range(5):
        system_log.log_system_event(f"event {i}")

    def _broken_replace(*args, **kwargs):
        raise OSError("simulated replace failure during trim")

    monkeypatch.setattr(system_log.os, "replace", _broken_replace)
    monkeypatch.setattr(system_log, "_MAX_FILE_SIZE", 1)  # force a trim on the next write

    event = system_log.log_system_event("triggers a trim that then fails")
    assert event["event"] == "triggers a trim that then fails"


def test_get_system_events_survives_a_read_failure(system_log, monkeypatch):
    system_log.log_system_event("one event")

    def _broken_open(path, *args, **kwargs):
        raise OSError("simulated read failure")

    monkeypatch.setattr(system_log, "open", _broken_open, raising=False)

    assert system_log.get_system_events() == []
