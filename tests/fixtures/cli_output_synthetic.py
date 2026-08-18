"""SYNTHETIC fixtures for `meshtastic` CLI output parsing tests.

These strings are NOT captured from a real radio - no real `--listen`/`--info`
session was recorded for this test suite. They were constructed to match the
shape the parsers in server.py actually expect (Python dict-repr lines for
`--listen`, a `Nodes in mesh: {...}` JSON block for `--info`), based on
reading server.py's own parsing code (process_received_nodeinfo_line,
parse_telemetry_from_listen_line, parse_nodes_from_info). If the real CLI's
output format ever drifts from what's encoded here, these tests will not
catch that - see CLAUDE.md's note that the CLI's human-readable log format is
the actual fragile dependency. Prefer replacing these with real captured
output (`meshtastic --listen`/`--info` logs) if/when it's convenient to grab
some from an actual node.
"""

# A "--listen" line for a remote node's periodic TELEMETRY_APP packet
# (deviceMetrics only - the common case).
LISTEN_LINE_DEVICE_TELEMETRY = (
    "DEBUG file:1179 Received: {'from': 2181570266, 'to': 4294967295, "
    "'decoded': {'portnum': 'TELEMETRY_APP', 'payload': b'...', "
    "'telemetry': {'time': 1755000000, 'deviceMetrics': "
    "{'batteryLevel': 85, 'voltage': 4.055, 'channelUtilization': 3.5, "
    "'airUtilTx': 1.2, 'uptimeSeconds': 12345}}}, 'id': 123456789, "
    "'rxTime': 1755000000, 'rxSnr': 7.5, 'hopLimit': 3, 'rxRssi': -60, "
    "'fromId': '!820af75a', 'toId': '^all'}"
)
LISTEN_LINE_DEVICE_TELEMETRY_EXPECTED = {
    "node_id": "!820af75a",
    "values": {
        "temperature": None,
        "humidity": None,
        "pressure": None,
        "voltage": 4.055,
        "current": None,
        "battery_level": 85.0,
        "channel_utilization": 3.5,
        "air_util_tx": 1.2,
        "uptime_seconds": 12345,
        "power_channels": None,
    },
}

# A "--listen" line for a remote node's ENVIRONMENT_METRICS_APP packet
# (BME280-style sensor: temperature/humidity/pressure, no device metrics).
LISTEN_LINE_ENVIRONMENT_TELEMETRY = (
    "DEBUG file:1179 Received: {'from': 2181570266, 'to': 4294967295, "
    "'decoded': {'portnum': 'TELEMETRY_APP', 'payload': b'...', "
    "'telemetry': {'time': 1755000100, 'environmentMetrics': "
    "{'temperature': 21.4, 'relativeHumidity': 55.2, "
    "'barometricPressure': 1013.25}}}, 'id': 123456790, "
    "'rxTime': 1755000100, 'rxSnr': 6.25, 'hopLimit': 3, 'rxRssi': -58, "
    "'fromId': '!820af75a', 'toId': '^all'}"
)
LISTEN_LINE_ENVIRONMENT_TELEMETRY_EXPECTED = {
    "node_id": "!820af75a",
    "values": {
        "temperature": 21.4,
        "humidity": 55.2,
        "pressure": 1013.25,
        "voltage": None,
        "current": None,
        "battery_level": None,
        "channel_utilization": None,
        "air_util_tx": None,
        "uptime_seconds": None,
        "power_channels": None,
    },
}

# A "--listen" line with no telemetry markers at all (a plain text message) -
# parse_telemetry_from_listen_line() must return None, not misparse it.
LISTEN_LINE_TEXT_MESSAGE = (
    "DEBUG file:1179 Received: {'from': 2181570266, 'to': 4294967295, "
    "'decoded': {'portnum': 'TEXT_MESSAGE_APP', 'payload': b'hello', "
    "'text': 'hello'}, 'id': 123456791, 'fromId': '!820af75a', "
    "'toId': '^all'}"
)

# A "Received nodeinfo: {...}" line, the shape process_received_nodeinfo_line()
# expects (a Python dict literal parsed via ast.literal_eval, not JSON - note
# the b'' bytes literal and single quotes, which JSON can't represent).
RECEIVED_NODEINFO_LINE = (
    "Received nodeinfo: {'num': 2181570266, 'user': {'id': '!820af75a', "
    "'longName': \"Flint's Test Node\", 'shortName': 'FTN1', "
    "'hwModel': 'RAK4631', 'role': 'CLIENT'}, "
    "'position': {'latitude': 52.520008, 'longitude': 13.404954, "
    "'altitude': 34, 'time': 1755000200}, "
    "'lastHeard': 1755000200, 'snr': 5.75, 'hopsAway': 2, "
    "'deviceMetrics': {'batteryLevel': 72, 'voltage': 3.98, "
    "'channelUtilization': 2.1, 'airUtilTx': 0.8, 'uptimeSeconds': 54321}}"
)

# A minimal "--info" output excerpt containing the "Nodes in mesh: {...}"
# JSON block parse_nodes_from_info() looks for (real `--info` output has a
# lot more text around this - only the parsed block matters here).
INFO_OUTPUT_NODES_IN_MESH = """
Connected to radio
Owner: Test Local Node (TEST)
Nodes in mesh: {
  "!820af75a": {
    "num": 2181570266,
    "user": {
      "id": "!820af75a",
      "longName": "Flint's Test Node",
      "shortName": "FTN1",
      "hwModel": "RAK4631",
      "role": "CLIENT"
    },
    "snr": 5.75,
    "lastHeard": 1755000200,
    "hopsAway": 2
  },
  "!aabbccdd": {
    "num": 2864434397,
    "user": {
      "id": "!aabbccdd",
      "longName": "Test Local Node",
      "shortName": "TEST",
      "hwModel": "RAK4631",
      "role": "ROUTER"
    }
  },
  "!00000000": {
    "num": 0,
    "user": {
      "id": "!00000000",
      "longName": "Unknown",
      "shortName": "UNK",
      "hwModel": "UNSET",
      "role": "CLIENT"
    }
  }
}
Preferences: {...}
"""
