"""Tests for api/api_hardware_i2c.py's register_hardware_i2c_routes() -
same flask test_client() pattern first used in test_api_waypoints.py.
register_hardware_i2c_routes(app, handle_errors, data_dir) is exactly the
interface worth testing as a whole: routing + JSON shape + the
reconcile-on-poll wiring, not just the underlying hardware/ functions
(already covered directly in test_hardware_i2c_service.py /
test_hardware_rtc_service.py / test_hardware_config.py).

No server.py import needed - only depends on api_hardware_i2c.py and the
hardware/ package it wires together, with hardware.i2c_service.scan_bus()/
hardware.rtc_service.get_status()/hardware.hardware_config's helper calls
all mocked - nothing here touches real hardware.
"""

from functools import wraps
from unittest.mock import patch

import pytest
from flask import Flask

from api.api_hardware_i2c import register_hardware_i2c_routes


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def api_env(tmp_path):
    app = Flask(__name__)
    register_hardware_i2c_routes(app, _handle_errors, str(tmp_path))
    return {"app": app, "client": app.test_client(), "data_dir": str(tmp_path)}


def test_get_i2c_status_shape(api_env):
    with patch(
        "hardware.i2c_service.scan_bus",
        return_value={"ok": True, "bus": 1, "addresses": ["0x68"]},
    ):
        response = api_env["client"].get("/api/hardware/i2c")

    assert response.status_code == 200
    body = response.get_json()
    assert body == {"ok": True, "bus": 1, "addresses": ["0x68"]}


def test_get_i2c_status_reports_scan_failure_reason(api_env):
    # The route transparently passes through scan_bus()'s own result dict -
    # {"ok": True, **scan} lets scan's own "ok" (False here) win, same
    # transparent-passthrough shape as the RTC route below. A scan failure
    # is a normal 200 response with ok:false + reason, not a 500 - only an
    # actual exception (caught by handle_errors) is a 500.
    with patch(
        "hardware.i2c_service.scan_bus",
        return_value={"ok": False, "bus": 1, "reason": "i2cdetect not installed"},
    ):
        response = api_env["client"].get("/api/hardware/i2c")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is False
    assert body["reason"] == "i2cdetect not installed"


def test_post_i2c_enable_success_reports_requires_reboot(api_env):
    with patch("hardware.hardware_config.enable_i2c", return_value={"ok": True, "stdout": "added"}):
        response = api_env["client"].post("/api/hardware/i2c/enable")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "requires_reboot": True}


def test_post_i2c_enable_failure_returns_500_with_reason(api_env):
    with patch(
        "hardware.hardware_config.enable_i2c",
        return_value={"ok": False, "reason": "sudo is not configured"},
    ):
        response = api_env["client"].post("/api/hardware/i2c/enable")

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "sudo is not configured" in body["error"]


def test_post_i2c_scan_returns_fresh_scan(api_env):
    with patch(
        "hardware.i2c_service.scan_bus",
        return_value={"ok": True, "bus": 1, "addresses": []},
    ):
        response = api_env["client"].post("/api/hardware/i2c/scan")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "bus": 1, "addresses": []}


def test_get_rtc_status_shape_with_no_pending(api_env):
    status = {
        "ok": True,
        "model": "ds3231",
        "display_name": "DS3231",
        "interface": "i2c",
        "bus": 1,
        "address": "0x68",
        "stages": {
            "detected": {"ok": True, "reason": None},
            "configured": {"ok": True, "reason": None},
            "readable": {"ok": True, "reason": None, "raw_output": "2026-08-21 23:59:59+00:00"},
        },
        "linux_device": "/dev/rtc0",
    }
    with patch("hardware.rtc_service.get_status", return_value=status), \
         patch("hardware.hardware_config.reconcile_pending", return_value=None):
        response = api_env["client"].get("/api/hardware/rtc")

    body = response.get_json()
    assert body["ok"] is True
    assert body["stages"]["detected"]["ok"] is True
    assert body["stages"]["configured"]["ok"] is True
    assert body["stages"]["readable"]["ok"] is True
    assert body["linux_device"] == "/dev/rtc0"
    assert body["pending_setup"] is None


def test_get_rtc_status_surfaces_pending_setup(api_env):
    pending_record = {"action": "configure_rtc", "model": "ds3231", "set_at": 1000.0}
    status = {"ok": True, "model": "ds3231", "stages": {}, "linux_device": None}
    with patch("hardware.rtc_service.get_status", return_value=status), \
         patch("hardware.hardware_config.reconcile_pending", return_value=None), \
         patch("hardware.hardware_config.get_pending", return_value=pending_record):
        response = api_env["client"].get("/api/hardware/rtc")

    body = response.get_json()
    assert body["pending_setup"] == pending_record


def test_get_rtc_status_unsupported_model_reports_error_not_500(api_env):
    with patch(
        "hardware.rtc_service.get_status",
        return_value={"ok": False, "reason": "unsupported RTC model: 'ds3231'"},
    ), patch("hardware.hardware_config.reconcile_pending", return_value=None), \
       patch("hardware.hardware_config.get_pending", return_value=None):
        response = api_env["client"].get("/api/hardware/rtc")

    body = response.get_json()
    assert body["ok"] is False
    assert "unsupported" in body["reason"]


def test_post_rtc_configure_success_reports_requires_reboot(api_env):
    with patch("hardware.hardware_config.configure_rtc", return_value={"ok": True, "stdout": "added"}):
        response = api_env["client"].post("/api/hardware/rtc/configure")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "requires_reboot": True}


def test_post_rtc_configure_failure_returns_500(api_env):
    with patch(
        "hardware.hardware_config.configure_rtc",
        return_value={"ok": False, "reason": "meshcenter-hw-config timed out after 15s"},
    ):
        response = api_env["client"].post("/api/hardware/rtc/configure")

    assert response.status_code == 500
    assert "timed out" in response.get_json()["error"]


def test_get_i2c_status_reconciles_pending_first(api_env):
    with patch("hardware.hardware_config.reconcile_pending") as mock_reconcile, \
         patch("hardware.i2c_service.scan_bus", return_value={"ok": True, "bus": 1, "addresses": []}):
        api_env["client"].get("/api/hardware/i2c")
    mock_reconcile.assert_called_once_with(api_env["data_dir"])
