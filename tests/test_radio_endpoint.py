"""Tests for meshsrv/radio_endpoint.py - the transport/endpoint shape
normalization and connect-orchestration helpers Radio TCP Transport part 2
introduces. No `meshtastic` import needed - this module is pure data-shape
logic over the RadioTransport ABC's own neutral models.
"""
from meshsrv.radio_endpoint import (
    DEFAULT_TCP_PORT,
    build_transport_connect_new,
    descriptor_from_radio_record,
    normalize_radio_record,
)
from meshsrv.radio_transport import ConnectionDescriptor, ConnectionState, ConnectionInfo, ConnectionType


# ---------------------------------------------------------------------------
# normalize_radio_record()
# ---------------------------------------------------------------------------

def test_legacy_serial_record_with_no_transport_key_normalizes_to_serial():
    legacy = {"node_id": "!756f9960", "long_name": "Flint TAP2", "port": "/dev/ttyACM0"}

    normalized = normalize_radio_record(legacy)

    assert normalized["transport"] == "serial"
    assert normalized["endpoint"] == {"port": "/dev/ttyACM0"}
    # The legacy flat field is preserved unchanged, not removed.
    assert normalized["port"] == "/dev/ttyACM0"
    assert normalized["node_id"] == "!756f9960"


def test_already_normalized_tcp_record_passes_through_unchanged():
    record = {
        "node_id": "!1fa065f0",
        "long_name": "T-Beam",
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }

    normalized = normalize_radio_record(record)

    assert normalized["transport"] == "tcp"
    assert normalized["endpoint"] == {"host": "192.168.2.34", "port": 4403}


def test_empty_record_normalizes_to_serial_with_empty_port():
    normalized = normalize_radio_record({})
    assert normalized["transport"] == "serial"
    assert normalized["endpoint"] == {"port": ""}


def test_none_record_normalizes_safely():
    normalized = normalize_radio_record(None)
    assert normalized["transport"] == "serial"
    assert normalized["endpoint"] == {"port": ""}


def test_transport_value_is_lowercased_and_stripped():
    record = {"transport": "  TCP  ", "endpoint": {"host": "192.168.2.34", "port": 4403}}
    normalized = normalize_radio_record(record)
    assert normalized["transport"] == "tcp"


def test_transport_key_present_but_no_endpoint_still_defaults_sensibly_for_tcp():
    normalized = normalize_radio_record({"transport": "tcp"})
    assert normalized["endpoint"] == {"host": "", "port": DEFAULT_TCP_PORT}


def test_transport_key_present_but_no_endpoint_defaults_sensibly_for_bluetooth():
    normalized = normalize_radio_record({"transport": "bluetooth"})
    assert normalized["endpoint"] == {"address": "", "label": ""}


# ---------------------------------------------------------------------------
# descriptor_from_radio_record()
# ---------------------------------------------------------------------------

def test_descriptor_from_legacy_serial_record():
    descriptor = descriptor_from_radio_record({"port": "/dev/ttyACM0"})
    assert descriptor.type == ConnectionType.SERIAL
    assert descriptor.address == "/dev/ttyACM0"


def test_descriptor_from_tcp_record():
    descriptor = descriptor_from_radio_record({
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    assert descriptor.type == ConnectionType.TCP
    assert descriptor.address == "192.168.2.34:4403"


def test_descriptor_from_tcp_record_defaults_port_when_missing():
    descriptor = descriptor_from_radio_record({"transport": "tcp", "endpoint": {"host": "192.168.2.34"}})
    assert descriptor.address == f"192.168.2.34:{DEFAULT_TCP_PORT}"


def test_descriptor_from_bluetooth_record():
    descriptor = descriptor_from_radio_record({
        "transport": "bluetooth",
        "endpoint": {"address": "3C:DC:75:6F:99:61", "label": "FLT2_9960"},
    })
    assert descriptor.type == ConnectionType.BLUETOOTH
    assert descriptor.address == "3C:DC:75:6F:99:61"
    assert descriptor.label == "FLT2_9960"


# ---------------------------------------------------------------------------
# build_transport_connect_new()
# ---------------------------------------------------------------------------

class _FakeTransport:
    def __init__(self, name):
        self.name = name
        self.connect_calls = []
        self.disconnect_calls = []

    def connect(self, descriptor, *, force, timeout):
        self.connect_calls.append((descriptor, force, timeout))
        return ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id="!1fa065f0")

    def disconnect(self, *, timeout):
        self.disconnect_calls.append(timeout)


def test_build_connect_new_for_serial_disconnects_the_other_two_and_connects_serial():
    serial, ble, tcp = _FakeTransport("serial"), _FakeTransport("ble"), _FakeTransport("tcp")

    connect_new = build_transport_connect_new(
        "serial",
        serial_transport=serial, ble_transport=ble, tcp_transport=tcp,
        serial_port="/dev/ttyACM0",
    )
    result = connect_new()

    assert result is serial
    assert len(serial.connect_calls) == 1
    assert serial.connect_calls[0][0].address == "/dev/ttyACM0"
    assert len(ble.disconnect_calls) == 1
    assert len(tcp.disconnect_calls) == 1
    assert serial.disconnect_calls == []  # never disconnects itself


def test_build_connect_new_for_tcp_disconnects_serial_and_ble_and_connects_tcp():
    serial, ble, tcp = _FakeTransport("serial"), _FakeTransport("ble"), _FakeTransport("tcp")

    connect_new = build_transport_connect_new(
        "tcp",
        serial_transport=serial, ble_transport=ble, tcp_transport=tcp,
        tcp_host="192.168.2.34", tcp_port=4403,
    )
    result = connect_new()

    assert result is tcp
    assert tcp.connect_calls[0][0].address == "192.168.2.34:4403"
    assert tcp.connect_calls[0][1] is True  # force=True
    assert len(serial.disconnect_calls) == 1
    assert len(ble.disconnect_calls) == 1
    assert tcp.disconnect_calls == []


def test_build_connect_new_for_bluetooth_disconnects_serial_and_tcp_and_connects_ble():
    serial, ble, tcp = _FakeTransport("serial"), _FakeTransport("ble"), _FakeTransport("tcp")

    connect_new = build_transport_connect_new(
        "bluetooth",
        serial_transport=serial, ble_transport=ble, tcp_transport=tcp,
        ble_address="3C:DC:75:6F:99:61", ble_name="FLT2_9960",
    )
    result = connect_new()

    assert result is ble
    assert ble.connect_calls[0][0].address == "3C:DC:75:6F:99:61"
    assert len(serial.disconnect_calls) == 1
    assert len(tcp.disconnect_calls) == 1


def test_build_connect_new_unknown_transport_raises_value_error():
    serial, ble, tcp = _FakeTransport("serial"), _FakeTransport("ble"), _FakeTransport("tcp")
    try:
        build_transport_connect_new(
            "carrier_pigeon", serial_transport=serial, ble_transport=ble, tcp_transport=tcp
        )
        assert False, "expected ValueError"
    except ValueError:
        pass
