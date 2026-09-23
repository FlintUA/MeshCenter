"""Regression coverage for the IPC JSON serialization hardening fix: all
three RadioTransport implementations' _to_node_info() must sanitize
NodeInfo.position through adapters.meshtastic._json_safe.json_safe()
before it can reach adapters/meshtastic/ipc_server.py's serve_forever() -
see that function's own hardening and _json_safe.py's json_safe()
docstring for the live-caught crash this guards against (a raw,
non-JSON-serializable protobuf object the installed meshtastic library
injects under position["raw"] for any POSITION_APP-decoded node, which
previously took down the whole adapter subprocess). See
tests/test_json_safe.py for json_safe() itself, in isolation.

One file covering all three transports deliberately, rather than three
separate additions scattered across each transport's own test file -
this is a single cross-cutting concern (all three share the identical
`position=json_safe(data.get("position"))` line), not three independent
behaviors.
"""
from adapters.meshtastic.ble_transport import BLETransport
from adapters.meshtastic.serial_transport import SerialTransport
from adapters.meshtastic.tcp_transport import TCPTransport


class _RawProtobufStandIn:
    """Stand-in for a real mesh_pb2.Position message object - doesn't
    need to actually be a protobuf type, just needs to not be JSON-
    serializable, matching what json_safe() is written to strip
    regardless of the concrete type."""

    def __repr__(self):
        return "<_RawProtobufStandIn>"


_TRANSPORTS = {
    "tcp": TCPTransport,
    "serial": SerialTransport,
    "ble": BLETransport,
}


def _raw_node_data(position):
    return {
        "num": 0x11223344,
        "user": {"id": "!11223344", "longName": "Remote Node", "shortName": "RMT"},
        "position": position,
    }


def test_to_node_info_strips_a_raw_protobuf_object_from_position_for_every_transport():
    position_with_raw = {
        "latitudeI": 507654321,
        "longitudeI": 303456789,
        "raw": _RawProtobufStandIn(),
    }
    for name, transport_cls in _TRANSPORTS.items():
        info = transport_cls._to_node_info("!11223344", _raw_node_data(position_with_raw))
        assert "raw" not in info.position, f"{name} transport still leaked raw protobuf object into position"
        assert info.position == {"latitudeI": 507654321, "longitudeI": 303456789}, name


def test_to_node_info_leaves_a_normal_position_dict_unchanged_for_every_transport():
    clean_position = {
        "latitudeI": 507654321,
        "longitudeI": 303456789,
        "altitude": 123,
        "latitude": 50.7654321,
        "longitude": 30.3456789,
    }
    for name, transport_cls in _TRANSPORTS.items():
        info = transport_cls._to_node_info("!11223344", _raw_node_data(dict(clean_position)))
        assert info.position == clean_position, name


def test_to_node_info_handles_a_node_with_no_position_at_all_for_every_transport():
    for name, transport_cls in _TRANSPORTS.items():
        info = transport_cls._to_node_info("!11223344", _raw_node_data(None))
        assert info.position is None, name
