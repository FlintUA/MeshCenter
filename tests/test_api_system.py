"""Tests for api/api_system.py's Wi-Fi scan route.

Covers the UX-gap fix: a failed scan (helper-level reason, or an
unexpected exception mid-parse) must (1) surface its real reason string
in the JSON response, unchanged behavior already present, and (2) now
also record a log_system_event() entry with source="wifi", the missing
half of the fix - the frontend's own display path is exercised in
static/chat.js only, not testable from here.
"""
import sys
import types
from unittest.mock import MagicMock

import pytest
from flask import Flask

if "config" not in sys.modules:
    _fake_config = types.ModuleType("config")
    _fake_config.DATA_DIR = "/tmp/meshcenter_test_data"
    sys.modules["config"] = _fake_config

from api.api_system import register_system_routes
import api.api_system as api_system_module
from meshsrv import network_config


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    register_system_routes(app)

    monkeypatch.setattr(api_system_module, "get_system_events", lambda **k: [])

    return app.test_client()


def test_wifi_scan_helper_failure_returns_reason_and_logs_it(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(
        network_config, "scan",
        lambda: {"ok": False, "reason": "sudo is not configured for meshcenter-network-helper"},
    )

    response = client.get("/api/system/wifi/scan")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "sudo is not configured for meshcenter-network-helper"

    assert len(logged) == 1
    assert logged[0]["source"] == "wifi"
    assert logged[0]["level"] == "ERROR"
    assert "sudo is not configured for meshcenter-network-helper" in logged[0]["details"]


def test_wifi_scan_unexpected_exception_returns_reason_and_logs_it(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )

    def _raise():
        raise RuntimeError("iw not found")

    monkeypatch.setattr(network_config, "scan", _raise)

    response = client.get("/api/system/wifi/scan")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    # The exception text no longer reaches the client: generic body + error_id...
    assert data["error"] == "Internal server error"
    assert data["error_code"] == "internal_error"
    assert "iw not found" not in response.get_data(as_text=True)
    assert data["networks"] == []

    # ...and the real cause is in the System Log under that same id.
    assert len(logged) == 1
    assert logged[0]["source"] == "wifi"
    assert logged[0]["level"] == "ERROR"
    assert data["error_id"] in logged[0]["details"]
    assert "iw not found" in logged[0]["details"]


@pytest.mark.parametrize("action,expected_title", [
    ("reboot", "Device reboot requested"),
    ("shutdown", "Device shutdown requested"),
])
def test_system_action_labels_are_generic_not_raspberry_pi(client, monkeypatch, action, expected_title):
    # These used to hardcode "Raspberry Pi reboot"/"Raspberry Pi shutdown" -
    # wrong on any non-Pi host (e.g. a Droidian phone) this project also
    # supports. Also spawns a background thread on success - stub it out so
    # the test doesn't actually try to sudo systemctl reboot/poweroff.
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "")
    monkeypatch.setattr(api_system_module.threading, "Thread", MagicMock())

    response = client.post("/api/system/action", json={"action": action})

    assert response.status_code == 202
    assert response.get_json()["message"] == expected_title
    assert len(logged) == 1
    assert logged[0]["title"] == expected_title
    assert "Raspberry Pi" not in logged[0]["title"]
    assert "Raspberry Pi" not in logged[0]["details"]


def test_system_action_log_includes_device_model_when_available(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "Google Inc. MSM sdm670 B4 PVT v1.0")
    monkeypatch.setattr(api_system_module.threading, "Thread", MagicMock())

    client.post("/api/system/action", json={"action": "reboot"})

    assert logged[0]["details"] == "Requested from MeshCenter web interface (Google Inc. MSM sdm670 B4 PVT v1.0)"


def test_system_action_log_omits_model_suffix_when_unavailable(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda title, level="INFO", details="", source="system": logged.append(
            {"title": title, "level": level, "details": details, "source": source}
        ),
    )
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "")
    monkeypatch.setattr(api_system_module.threading, "Thread", MagicMock())

    client.post("/api/system/action", json={"action": "reboot"})

    assert logged[0]["details"] == "Requested from MeshCenter web interface"


def test_wifi_scan_success_path_does_not_log(client, monkeypatch):
    logged = []
    monkeypatch.setattr(
        api_system_module, "log_system_event",
        lambda *a, **k: logged.append((a, k)),
    )
    monkeypatch.setattr(network_config, "scan", lambda: {"ok": True, "stdout": ""})
    monkeypatch.setattr(network_config, "list_wifi_connections", lambda: {"ok": True, "ssids": set()})

    response = client.get("/api/system/wifi/scan")

    assert response.status_code == 200
    data = response.get_json()
    assert data["ok"] is True
    assert data["networks"] == []
    assert logged == []


def test_system_info_model_field_uses_get_device_model(client, monkeypatch):
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "Google Inc. MSM sdm670 B4 PVT v1.0")

    response = client.get("/api/system/info")

    assert response.status_code == 200
    assert response.get_json()["model"] == "Google Inc. MSM sdm670 B4 PVT v1.0"


def test_system_info_model_field_is_none_when_unavailable(client, monkeypatch):
    monkeypatch.setattr(api_system_module, "get_device_model", lambda: "")

    response = client.get("/api/system/info")

    assert response.get_json()["model"] is None


def test_system_routes_handle_errors_sanitizes_uncaught_exceptions():
    from meshsrv import error_reporting

    def mock_handle_errors(f):
        def wrapper(*args, **kwargs):
            try:
                return f(*args, **kwargs)
            except Exception as exc:
                error_id = error_reporting.report_unexpected_error(exc, "test_context")
                return error_reporting.internal_error_payload(error_id), 500
        wrapper.__name__ = f.__name__
        return wrapper

    app = Flask(__name__)
    register_system_routes(app, handle_errors=mock_handle_errors)
    test_client = app.test_client()

    # Trigger an exception in a system route by raising inside get_all
    with pytest.MonkeyPatch.context() as m:
        m.setattr("meshsrv.notification_service.get_all", lambda: 1 / 0)
        response = test_client.get("/api/notifications")

    assert response.status_code == 500
    data = response.get_json()
    assert data["ok"] is False
    assert data["error"] == "Internal server error"
    assert data["error_code"] == "internal_error"
    assert "error_id" in data
    assert "ZeroDivisionError" not in response.get_data(as_text=True)
