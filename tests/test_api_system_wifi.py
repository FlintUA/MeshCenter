def test_wifi_endpoints_ssid_validation(server_module):
    client = server_module.app.test_client()

    rules = [rule.rule for rule in server_module.app.url_map.iter_rules()]
    assert "/api/system/wifi/connect" in rules
    assert "/api/system/wifi/forget" in rules

    # Invalid SSIDs should be rejected with 400
    for invalid_ssid in ["-option", "--delete", "Test\nSSID", "a" * 65]:
        resp_conn = client.post("/api/system/wifi/connect", json={"ssid": invalid_ssid})
        assert resp_conn.status_code == 400, f"Expected 400 for connect, got {resp_conn.status_code}"
        data_conn = resp_conn.get_json()
        assert data_conn["ok"] is False

        resp_forget = client.post("/api/system/wifi/forget", json={"ssid": invalid_ssid})
        assert resp_forget.status_code == 400, f"Expected 400 for forget, got {resp_forget.status_code}"
        data_forget = resp_forget.get_json()
        assert data_forget["ok"] is False

    # Empty SSID should also return 400
    resp_empty = client.post("/api/system/wifi/connect", json={"ssid": ""})
    assert resp_empty.status_code == 400
