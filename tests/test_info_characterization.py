"""F3 characterization test: the fixed --info parsers (get_telemetry_from_info,
update_base_status_from_info, parse_nodes_from_info) against REAL captured
`meshtastic --info` output from a real radio (Flint TAP2, dev/.104), not a
hand-constructed synthetic string.

tests/fixtures/info_real_tap2_redacted.txt is that capture with secrets
redacted (psk/publicKey/privateKey/deviceId/MQTT password -> same-length
'X' placeholders, channel URLs stripped, lat/lon zeroed) and every
third-party node's name/shortName/macaddr/node-id replaced with synthetic
values that preserve the real capture's tricky shapes (umlauts, an emoji,
a literal '|', a trailing space, a URL-shaped name) without reusing any
fragment of a real name, place, or id. Flint's own two real nodes in the
capture (Flint TAP2 - local - and Elektroniker.help) keep their real
names/ids (their MAC addresses are redacted too). See the redaction
script's own commit message/PR description for the exact mapping.

This capture's local node (Flint TAP2) already has its own complete
deviceMetrics and no neighbour has environmentMetrics/powerMetrics at
all, so defects A/B's specific "local missing a block a neighbour has"
scenario is not present in this real data - nothing to freeze or report
per the task's characterization caveat; that scenario is already covered
directly by the synthetic fixtures in test_cli_parsing.py.
"""

from pathlib import Path

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "info_real_tap2_redacted.txt"
REAL_INFO_OUTPUT = FIXTURE_PATH.read_text(encoding="utf-8")

LOCAL_NODE_ID = "!756f9960"


def test_parse_nodes_from_info_imports_every_neighbour(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "LOCAL_NODE_ID", LOCAL_NODE_ID)

    changed = server_module.parse_nodes_from_info(REAL_INFO_OUTPUT)
    assert changed is True

    # The local node itself must never be imported as if it were a neighbour.
    assert LOCAL_NODE_ID not in server_module.nodes

    # Every one of the 13 synthetic third-party nodes must be imported -
    # this is the real-world equivalent of defect C's brace-name repro:
    # one of them (f0000005) has a longName containing an emoji, and
    # another (f0000006) contains a literal '|' - neither may break the
    # import of ANY of the others.
    expected_ids = {
        "!f0000001", "!f0000002", "!f0000003", "!f0000004", "!f0000005",
        "!f0000006", "!f0000007", "!f0000008", "!f0000009", "!f000000a",
        "!f000000b", "!f000000c", "!f000000d",
        "!1fa065f0",  # Elektroniker.help - Flint's own second real node
    }
    assert expected_ids <= set(server_module.nodes.keys())


def test_parse_nodes_from_info_preserves_unicode_and_pipe_names(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "LOCAL_NODE_ID", LOCAL_NODE_ID)
    server_module.parse_nodes_from_info(REAL_INFO_OUTPUT)

    # Emoji + umlaut name.
    assert server_module.nodes["!f0000005"]["name"] == "22TS451 Basisstation Mürnitz\U0001F340"
    # Literal '|' in the name.
    assert server_module.nodes["!f0000006"]["name"] == "knoten.test | Base Nord"
    # A URL-shaped name.
    assert server_module.nodes["!f0000007"]["name"] == "https://www.exmstr.test/panel"


def test_get_telemetry_from_info_uses_only_local_deviceMetrics(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "LOCAL_NODE_ID", LOCAL_NODE_ID)
    captured = {}

    def _capture(values, save_history=True):
        captured["values"] = values
        return True

    monkeypatch.setattr(server_module, "apply_telemetry_values", _capture)
    server_module.get_telemetry_from_info(REAL_INFO_OUTPUT)

    assert "values" in captured
    values = captured["values"]
    # Flint TAP2's own real (redaction-untouched) deviceMetrics.
    assert values["voltage"] == 4.059
    assert values["battery_level"] == 92
    # No environmentMetrics/powerMetrics exist anywhere in this real
    # capture (not even on a neighbour) - so these must stay None, not
    # picked up from any of the 13 neighbours' own deviceMetrics.
    assert values["temperature"] is None
    assert values["humidity"] is None
    assert values["pressure"] is None
    assert values["current"] is None


def test_update_base_status_from_info_uses_only_local_deviceMetrics(server_module, monkeypatch):
    monkeypatch.setattr(server_module, "LOCAL_NODE_ID", LOCAL_NODE_ID)
    server_module.update_base_status_from_info(REAL_INFO_OUTPUT)

    status = server_module.base_status
    assert status["voltage"] == 4.059
    assert status["battery_level"] == 92
    assert status["channel_utilization"] == 2.5433333
    assert status["air_util_tx"] == 0.69294447
    assert status["uptime_seconds"] == 4001440
