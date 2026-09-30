"""Tests for server.py's `meshtastic` CLI stdout parsers.

Fixture strings live in tests/fixtures/cli_output_synthetic.py and are
explicitly marked as synthetic (constructed from reading the parsing code,
not captured from a real radio) - see that file's docstring.
"""

from fixtures.cli_output_synthetic import (
    INFO_OUTPUT_LOCAL_METRICS_LEAK,
    INFO_OUTPUT_LOCAL_MISSING_DEVICE_METRICS,
    INFO_OUTPUT_NODE_NAME_WITH_BRACE,
    INFO_OUTPUT_NODES_IN_MESH,
    LISTEN_LINE_DEVICE_TELEMETRY,
    LISTEN_LINE_DEVICE_TELEMETRY_EXPECTED,
    LISTEN_LINE_ENVIRONMENT_TELEMETRY,
    LISTEN_LINE_ENVIRONMENT_TELEMETRY_EXPECTED,
    LISTEN_LINE_TEXT_MESSAGE,
    RECEIVED_NODEINFO_LINE,
)


def test_parse_telemetry_device_metrics(server_module):
    result = server_module.parse_telemetry_from_listen_line(LISTEN_LINE_DEVICE_TELEMETRY)
    assert result == LISTEN_LINE_DEVICE_TELEMETRY_EXPECTED


def test_parse_telemetry_environment_metrics(server_module):
    result = server_module.parse_telemetry_from_listen_line(LISTEN_LINE_ENVIRONMENT_TELEMETRY)
    assert result == LISTEN_LINE_ENVIRONMENT_TELEMETRY_EXPECTED


def test_parse_telemetry_ignores_non_telemetry_lines(server_module):
    assert server_module.parse_telemetry_from_listen_line(LISTEN_LINE_TEXT_MESSAGE) is None


def test_parse_telemetry_battery_level_clamped_to_100(server_module):
    # deviceMetrics.batteryLevel can report >100 for a plugged-in/charging
    # node (observed live) - parse_telemetry_from_listen_line() clamps it.
    line = LISTEN_LINE_DEVICE_TELEMETRY.replace("'batteryLevel': 85", "'batteryLevel': 101")
    result = server_module.parse_telemetry_from_listen_line(line)
    assert result["values"]["battery_level"] == 100.0


def test_process_received_nodeinfo_line_updates_node_state(server_module):
    handled = server_module.process_received_nodeinfo_line(RECEIVED_NODEINFO_LINE)
    assert handled is True

    node = server_module.nodes["!820af75a"]
    assert node["name"] == "Flint's Test Node"
    assert node["short_name"] == "FTN1"
    assert node["hw_model"] == "RAK4631"
    assert node["role"] == "CLIENT"
    assert node["snr"] == 5.75
    assert node["hop_start"] == "2"
    assert node["position"]["latitude"] == 52.520008
    assert node["position"]["longitude"] == 13.404954


def test_process_received_nodeinfo_line_ignores_unrelated_lines(server_module):
    assert server_module.process_received_nodeinfo_line("some unrelated log line") is False


def test_process_received_nodeinfo_line_ignores_malformed_payload(server_module):
    # Payload present but not a valid Python literal - must not raise.
    assert server_module.process_received_nodeinfo_line("Received nodeinfo: {not valid python") is False


def test_parse_nodes_from_info_imports_known_nodes(server_module):
    changed = server_module.parse_nodes_from_info(INFO_OUTPUT_NODES_IN_MESH)
    assert changed is True

    node = server_module.nodes["!820af75a"]
    assert node["name"] == "Flint's Test Node"
    assert node["short_name"] == "FTN1"
    assert node["hop_start"] == "2"


def test_parse_nodes_from_info_skips_local_node(server_module):
    server_module.parse_nodes_from_info(INFO_OUTPUT_NODES_IN_MESH)
    # LOCAL_NODE_ID ("!aabbccdd" in the test config) must never be imported
    # as if it were a remote node.
    assert "!aabbccdd" not in server_module.nodes


def test_parse_nodes_from_info_skips_unknown_placeholder_name(server_module):
    server_module.parse_nodes_from_info(INFO_OUTPUT_NODES_IN_MESH)
    # longName "Unknown" is a Meshtastic placeholder for an undiscovered
    # node - importing it would create a permanent "Unknown" chat entry.
    assert "!00000000" not in server_module.nodes


def test_parse_nodes_from_info_returns_false_without_marker(server_module):
    assert server_module.parse_nodes_from_info("no nodes-in-mesh marker here") is False


# ---------------------------------------------------------------------------
# F3 defect A: get_telemetry_from_info() must read the three metric blocks
# ONLY from the local node's own entry, never a neighbour's.
# ---------------------------------------------------------------------------

def test_get_telemetry_from_info_never_applies_a_neighbours_environment_metrics(server_module):
    captured = {}

    def _capture(values, save_history=True):
        captured["values"] = values
        return True

    server_module.apply_telemetry_values = _capture
    server_module.get_telemetry_from_info(INFO_OUTPUT_LOCAL_METRICS_LEAK)

    assert "values" in captured, "apply_telemetry_values() was never called"
    values = captured["values"]
    # The neighbour's environmentMetrics (temperature=99) must never appear -
    # the local node itself has no environmentMetrics block at all.
    assert values["temperature"] is None
    assert values["humidity"] is None
    assert values["pressure"] is None
    # The local node's own deviceMetrics.voltage must still be applied.
    assert values["voltage"] == 4.1


# ---------------------------------------------------------------------------
# F3 defect B: update_base_status_from_info() must read deviceMetrics ONLY
# from the local node's own entry, never a neighbour's.
# ---------------------------------------------------------------------------

def test_update_base_status_from_info_ignores_neighbours_device_metrics_when_local_has_none(server_module):
    server_module.update_base_status_from_info(INFO_OUTPUT_LOCAL_MISSING_DEVICE_METRICS)
    # The neighbour's voltage=3.3/battery=7 must never leak into base_status -
    # the local node has no deviceMetrics block at all, so base_status must
    # stay at its untouched defaults, not the neighbour's values.
    assert server_module.base_status.get("voltage") != 3.3
    assert server_module.base_status.get("battery_level") != 7


def test_update_base_status_from_info_uses_exact_local_values_when_present(server_module):
    server_module.update_base_status_from_info(INFO_OUTPUT_LOCAL_METRICS_LEAK)
    assert server_module.base_status["voltage"] == 4.1
    assert server_module.base_status["battery_level"] == 80
    assert server_module.base_status["channel_utilization"] == 2.5
    assert server_module.base_status["air_util_tx"] == 1.1
    assert server_module.base_status["uptime_seconds"] == 9999


# ---------------------------------------------------------------------------
# F3 defect C: a node name containing '}' must not break the whole NodeDB
# import - only that one node's parsing/name is affected, never its
# neighbours.
# ---------------------------------------------------------------------------

def test_parse_nodes_from_info_node_name_with_brace_does_not_block_normal_neighbour(server_module):
    changed = server_module.parse_nodes_from_info(INFO_OUTPUT_NODE_NAME_WITH_BRACE)
    assert changed is True
    assert "!ffffffff" in server_module.nodes
    assert server_module.nodes["!ffffffff"]["name"] == "Normal Neighbour"
