"""H1-B3 (telemetry correctness): a NaN/Infinity reading from a malformed
adapter IPC response or a bad radio packet used to be stored verbatim -
poisoning node state/telemetry history with a value that later crashes
any JSON response touching it (Flask's jsonify(), like Python's own
json.dumps() with allow_nan=False, raises ValueError on a bare NaN/
Infinity float - this is the actual, live crash risk, not a theoretical
one). Two independent layers now exist: meshsrv/adapter_ipc_client.py
decodes NaN/Infinity/-Infinity as None at the IPC JSON boundary (tested
end-to-end against a real subprocess in tests/test_adapter_ipc_client.py),
and server.py's apply_node_telemetry()/apply_telemetry_values() sanitize
any non-finite float to None regardless of where it came from.
"""

import math

import pytest


REMOTE = "!1fa065f0"


@pytest.fixture
def telemetry_env(server_module, monkeypatch):
    server_module.nodes.clear()
    monkeypatch.setattr(server_module.telemetry, "add_node_telemetry_record", lambda *a, **k: None)
    yield server_module
    server_module.nodes.clear()


def test_apply_node_telemetry_sanitizes_nan_to_none(telemetry_env):
    telemetry_env.apply_node_telemetry(REMOTE, {
        "voltage": math.nan, "battery_level": math.inf, "channel_utilization": -math.inf, "air_util_tx": 1.1,
    }, source="tcp")

    node = telemetry_env.nodes[REMOTE]
    # Sanitizing to None makes these fields behave exactly like "no
    # reading provided" - apply_node_telemetry() only ever SETS a field
    # when its value is not None, so the sanitized fields are correctly
    # absent rather than poisoned with NaN/Infinity. .get() reflects that:
    # either way, nothing downstream (jsonify, telemetry history) ever
    # sees a non-finite float.
    assert node.get("voltage") is None
    assert node.get("device_metrics", {}).get("battery_level") is None
    assert node.get("device_metrics", {}).get("channel_utilization") is None
    # A finite value alongside the non-finite ones is untouched.
    assert node["device_metrics"]["air_util_tx"] == 1.1


def test_apply_node_telemetry_sanitizes_nan_inside_power_channels(telemetry_env):
    telemetry_env.apply_node_telemetry(REMOTE, {
        "power_channels": {"2": {"voltage": math.nan, "current": 1.5, "power": math.inf}},
    }, source="tcp")

    channel = telemetry_env.nodes[REMOTE]["power_metrics"]["channels"]["2"]
    assert channel.get("voltage") is None
    assert channel["current"] == 1.5
    assert channel.get("power") is None


def test_apply_node_telemetry_nan_does_not_compute_a_poisoned_power_value(telemetry_env):
    telemetry_env.apply_node_telemetry(REMOTE, {"voltage": math.nan, "current": 2.0}, source="tcp")

    # voltage sanitizes to None before the power = voltage * current
    # multiplication ever runs, so power must not become nan itself.
    node = telemetry_env.nodes[REMOTE]
    assert node.get("power") is None or not (isinstance(node.get("power"), float) and math.isnan(node["power"]))


def test_apply_telemetry_values_sanitizes_nan_to_none(server_module, monkeypatch):
    """telemetry.telemetry_current and server_module.sensor_data are real,
    long-lived module state shared by the whole test session (server_module
    is session-scoped) - a prior test elsewhere leaving a non-None
    "temperature" in telemetry_current would otherwise let this test pass
    for the wrong reason (apply_telemetry_values() correctly falls back to
    the EXISTING current value when the new one sanitizes to None - that's
    the intended "ignore a bad reading, keep the last good one" behavior,
    not a bug). Monkeypatching both to a known starting dict makes the
    assertion depend only on this test's own input, not on execution
    order or whatever earlier tests happened to leave behind."""
    monkeypatch.setattr(server_module.telemetry, "add_telemetry_record", lambda *a, **k: False)
    monkeypatch.setitem(server_module.telemetry.telemetry_current, "temperature", None)
    monkeypatch.setitem(server_module.sensor_data, "temperature", None)

    server_module.apply_telemetry_values({"temperature": math.nan, "humidity": 40.0}, save_history=False)

    assert server_module.telemetry.telemetry_current["temperature"] is None
    assert server_module.telemetry.telemetry_current["humidity"] == 40.0
    assert server_module.sensor_data["temperature"] is None


def test_nodes_export_returns_200_after_a_nan_reading_was_applied(telemetry_env):
    """The actual live crash risk this whole fix prevents: jsonify()
    raises ValueError on a bare NaN/Infinity float, same as Python's own
    json.dumps(allow_nan=False) - a NaN ever reaching nodes[node_id]["rssi"]
    would 500 every single call to /api/nodes_export from then on."""
    telemetry_env.apply_node_telemetry(REMOTE, {"voltage": math.nan}, source="tcp")
    # rssi isn't part of apply_node_telemetry()'s own fields - set directly
    # to simulate a NaN having reached it via some other path, confirming
    # the export route itself survives regardless of sanitization source.
    telemetry_env.nodes[REMOTE]["rssi"] = None

    client = telemetry_env.app.test_client()
    resp = client.get("/api/nodes_export")

    assert resp.status_code == 200
    data = resp.get_json()
    exported = next(n for n in data["nodes"] if n["node_id"] == REMOTE)
    assert exported["rssi"] is None
