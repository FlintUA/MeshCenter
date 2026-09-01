"""Tests for server.py's verify_radio_identity() - the log_system_event()/
push_notification() calls it now makes after computing configured/detected
labels, previously only printed to stdout (see this fix's own PR: the
Instance card was showing a raw subprocess.TimeoutExpired string directly
to the user - the fix moves the detail into the System Log instead and
only shows a short hint + a link on the card, see static/chat.js's
loadInstanceInfo()). Uses the server_module fixture since verify_radio_identity()
depends on several server.py-level globals (MESHTASTIC_CMD, INSTANCE_IDENTITY,
instance_manager) that only exist once server.py has actually been imported.
"""

import pytest

import meshsrv.notification_service as notification_service


@pytest.fixture
def _preserve_instance_state(server_module):
    # verify_radio_identity() mutates INSTANCE_IDENTITY/RADIO_IDENTITY_RESULT
    # (module globals) and writes through instance_manager - none of that is
    # covered by conftest.py's autouse _reset_server_state fixture, so
    # restore it explicitly to avoid leaking state into other test files
    # that share the session-scoped server_module fixture.
    original_identity = server_module.instance_manager.get()
    original_radio_result = dict(server_module.RADIO_IDENTITY_RESULT)
    yield
    server_module.instance_manager.save(original_identity)
    server_module.INSTANCE_IDENTITY = original_identity
    server_module.RADIO_IDENTITY_RESULT = original_radio_result


def test_mismatch_logs_error_and_pushes_a_warning_notification(server_module, _preserve_instance_state, monkeypatch):
    logged = []
    notified = []
    monkeypatch.setattr(
        server_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(
        notification_service, "push_notification",
        lambda **kwargs: notified.append(kwargs),
    )
    monkeypatch.setattr(
        server_module, "detect_radio_identity",
        lambda *a, **k: (
            {
                "status": "MISMATCH",
                "checked_at": "2026-09-01T00:00:00+00:00",
                "configured": {},
                "detected": {"node_id": "!deadbeef", "long_name": "Someone Else's Radio"},
                "error": None,
            },
            "raw --info output",
        ),
    )

    server_module.verify_radio_identity()

    assert len(logged) == 1
    assert logged[0]["title"] == "Radio identity mismatch"
    assert logged[0]["level"] == "ERROR"
    assert logged[0]["source"] == "identity"
    assert "!deadbeef" in logged[0]["details"]
    assert "Someone Else's Radio" in logged[0]["details"]

    assert len(notified) == 1
    assert notified[0]["level"] == "warning"
    assert notified[0]["source"] == "radio"
    assert "!deadbeef" in notified[0]["body"]


def test_detection_error_logs_warning_without_a_notification(server_module, _preserve_instance_state, monkeypatch):
    logged = []
    notified = []
    monkeypatch.setattr(
        server_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(
        notification_service, "push_notification",
        lambda **kwargs: notified.append(kwargs),
    )
    raw_error = (
        "Command '['/home/flint/meshcenter/adapters/meshtastic/venv/bin/meshtastic', "
        "'--port', '/dev/ttyACM0', '--info']' timed out after 25 seconds"
    )
    monkeypatch.setattr(
        server_module, "detect_radio_identity",
        lambda *a, **k: (
            {
                "status": "DETECTION_ERROR",
                "checked_at": "2026-09-01T00:00:00+00:00",
                "configured": {},
                "detected": {},
                "error": raw_error,
            },
            "",
        ),
    )

    server_module.verify_radio_identity()

    # This is the actual bug this fix closes: the raw subprocess timeout
    # text must land in the System Log (full detail preserved there for
    # diagnostics)...
    assert len(logged) == 1
    assert logged[0]["title"] == "Radio identity check failed"
    assert logged[0]["level"] == "WARNING"
    assert logged[0]["source"] == "identity"
    assert logged[0]["details"] == raw_error

    # ...but a transient startup failure is common enough that it must NOT
    # also pop a Notification on every single service start - that would
    # be pure noise, unlike a genuine MISMATCH.
    assert notified == []


def test_match_neither_logs_nor_notifies(server_module, _preserve_instance_state, monkeypatch):
    logged = []
    notified = []
    monkeypatch.setattr(
        server_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(
        notification_service, "push_notification",
        lambda **kwargs: notified.append(kwargs),
    )
    monkeypatch.setattr(
        server_module, "detect_radio_identity",
        lambda *a, **k: (
            {
                "status": "MATCH",
                "checked_at": "2026-09-01T00:00:00+00:00",
                "configured": {},
                "detected": {"node_id": "!aabbccdd", "long_name": "Test Local Node"},
                "error": None,
            },
            "raw --info output",
        ),
    )

    server_module.verify_radio_identity()

    assert logged == []
    assert notified == []
