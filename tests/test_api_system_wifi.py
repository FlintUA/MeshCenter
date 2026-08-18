"""Tests for Wi-Fi security validation in api/api_system.py.

Verifies that input validation prevents command option injection and control
character injection when SSID inputs are processed for system Wi-Fi commands.
"""


def test_wifi_connect_rejects_invalid_ssids(server_module):
    client = server_module.app.test_client()

    # Missing/empty SSID
    res = client.post("/api/system/wifi/connect", json={"ssid": ""})
    assert res.status_code == 400
    assert res.json.get("ok") is False

    # Option injection attempts starting with '-'
    for invalid_ssid in ("--help", "-f", "--active", "--version", "-a"):
        res = client.post("/api/system/wifi/connect", json={"ssid": invalid_ssid, "password": "pass"})
        assert res.status_code == 400
        assert res.json.get("ok") is False
        assert res.json.get("error") == "Invalid SSID"

    # Control character injection attempts
    for invalid_ssid in ("MyWiFi\x00", "MyWiFi\nInjected", "MyWiFi\rInjected"):
        res = client.post("/api/system/wifi/connect", json={"ssid": invalid_ssid})
        assert res.status_code == 400
        assert res.json.get("ok") is False
        assert res.json.get("error") == "Invalid SSID"


def test_wifi_forget_rejects_invalid_ssids(server_module):
    client = server_module.app.test_client()

    # Missing/empty SSID
    res = client.post("/api/system/wifi/forget", json={"ssid": ""})
    assert res.status_code == 400
    assert res.json.get("ok") is False

    # Option injection attempts starting with '-'
    for invalid_ssid in ("--help", "-f", "--delete", "-r"):
        res = client.post("/api/system/wifi/forget", json={"ssid": invalid_ssid})
        assert res.status_code == 400
        assert res.json.get("ok") is False
        assert res.json.get("error") == "Invalid SSID"

    # Control character injection attempts
    for invalid_ssid in ("HomeNet\x00", "HomeNet\n", "HomeNet\r"):
        res = client.post("/api/system/wifi/forget", json={"ssid": invalid_ssid})
        assert res.status_code == 400
        assert res.json.get("ok") is False
        assert res.json.get("error") == "Invalid SSID"
