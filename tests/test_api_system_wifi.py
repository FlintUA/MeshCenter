"""Tests for api/api_system.py's Wi-Fi SSID input validation logic."""

from unittest.mock import patch
from flask import Flask


def test_api_system_wifi_connect_invalid_ssid(server_module):
    from api.api_system import register_system_routes

    app = Flask(__name__)
    register_system_routes(app)
    client = app.test_client()

    # Empty SSID
    resp = client.post("/api/system/wifi/connect", json={"ssid": ""})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid SSID format"

    # Option-injection leading hyphen
    resp = client.post("/api/system/wifi/connect", json={"ssid": "--create-profile"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid SSID format"

    # Exceeding 32 octets limit
    resp = client.post("/api/system/wifi/connect", json={"ssid": "a" * 33})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "SSID exceeds 32 octets limit"

    # Control characters
    resp = client.post("/api/system/wifi/connect", json={"ssid": "MyWiFi\x00Name"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "SSID contains invalid control characters"


def test_api_system_wifi_forget_invalid_ssid(server_module):
    from api.api_system import register_system_routes

    app = Flask(__name__)
    register_system_routes(app)
    client = app.test_client()

    # Option-injection leading hyphen
    resp = client.post("/api/system/wifi/forget", json={"ssid": "-d"})
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid SSID format"


def test_api_system_wifi_connect_valid_ssid(server_module):
    from api.api_system import register_system_routes

    app = Flask(__name__)
    register_system_routes(app)
    client = app.test_client()

    with patch("subprocess.check_output", return_value="Device 'wlan0' successfully activated"):
        resp = client.post("/api/system/wifi/connect", json={"ssid": "HomeNetwork", "password": "secretpassword"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True
