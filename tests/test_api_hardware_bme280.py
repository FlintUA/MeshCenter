"""Tests for api/api_hardware_bme280.py's register_hardware_bme280_routes()
- same flask test_client() pattern as test_api_hardware_i2c.py. The single
route just passes through hardware.bme280_service.get_status()'s own
result dict (mocked here), same style as the RTC/I2C routes.
"""

from functools import wraps
from unittest.mock import patch

import pytest
from flask import Flask

from api.api_hardware_bme280 import register_hardware_bme280_routes


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500
    return wrapped


@pytest.fixture
def api_env():
    app = Flask(__name__)
    register_hardware_bme280_routes(app, _handle_errors)
    return {"app": app, "client": app.test_client()}


def test_get_bme280_status_detected_and_readable(api_env):
    status = {
        "ok": True,
        "bus": 1,
        "address": "0x76",
        "stages": {"detected": {"ok": True}, "readable": {"ok": True, "reason": None}},
        "values": {"temperature_c": 22.4, "humidity_pct": 41.2, "pressure_hpa": 1013.2},
    }
    with patch("hardware.bme280_service.get_status", return_value=status):
        response = api_env["client"].get("/api/hardware/bme280")

    assert response.status_code == 200
    assert response.get_json() == status


def test_get_bme280_status_not_detected(api_env):
    status = {
        "ok": True,
        "bus": 1,
        "stages": {"detected": {"ok": False, "reason": "no device answered at 0x76 or 0x77"}},
    }
    with patch("hardware.bme280_service.get_status", return_value=status):
        response = api_env["client"].get("/api/hardware/bme280")

    body = response.get_json()
    assert body["stages"]["detected"]["ok"] is False
    assert "values" not in body
