"""Tests for api/api_system.py's Wi-Fi connection endpoint password redaction."""

import subprocess
from unittest.mock import patch

import pytest
from flask import Flask


@pytest.fixture
def api_client(server_module):
    from api.api_system import register_system_routes

    app = Flask(__name__)
    register_system_routes(app)
    return app.test_client()


def test_wifi_connect_redacts_password_on_called_process_error(api_client):
    cmd = ["sudo", "-n", "/usr/bin/nmcli", "dev", "wifi", "connect", "TestSSID", "password", "SuperSecretPass123!"]
    err = subprocess.CalledProcessError(1, cmd, output=None)

    with patch("subprocess.check_output", side_effect=err):
        response = api_client.post(
            "/api/system/wifi/connect",
            json={"ssid": "TestSSID", "password": "SuperSecretPass123!"},
        )

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "SuperSecretPass123!" not in body["error"]
    assert "******" in body["error"]


def test_wifi_connect_redacts_password_on_timeout_expired(api_client):
    cmd = ["sudo", "-n", "/usr/bin/nmcli", "dev", "wifi", "connect", "TestSSID", "password", "SuperSecretPass123!"]
    err = subprocess.TimeoutExpired(cmd=cmd, timeout=30)

    with patch("subprocess.check_output", side_effect=err):
        response = api_client.post(
            "/api/system/wifi/connect",
            json={"ssid": "TestSSID", "password": "SuperSecretPass123!"},
        )

    assert response.status_code == 500
    body = response.get_json()
    assert body["ok"] is False
    assert "SuperSecretPass123!" not in body["error"]
    assert "******" in body["error"]


def test_wifi_connect_success(api_client):
    with patch("subprocess.check_output", return_value="Device 'wlan0' successfully activated with 'TestSSID'.\n"):
        response = api_client.post(
            "/api/system/wifi/connect",
            json={"ssid": "TestSSID", "password": "SuperSecretPass123!"},
        )

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert "successfully activated" in body["message"]
