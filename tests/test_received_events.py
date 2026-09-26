"""ReceivedTextEvent / ReceivedWaypointEvent, their IPC serialization, and the
optional RadioTransport.drain_received().

The library's inbound packet dict carries protobuf/bytes fields (`raw`,
`decoded.payload`, and a second `raw` inside `decoded.waypoint`) that must never
cross the IPC boundary. These tests pin that: the events accept only plain
types, the serializer is an explicit whitelist, and nothing else can ride along.
"""
import dataclasses
import json

import pytest

from meshsrv import ipc_protocol
from meshsrv.radio_transport import (
    ConnectionInfo,
    ConnectionState,
    RadioTransport,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
    TransportError,
    TransportErrorCode,
)

TEXT_FIELDS = [
    "packet_id", "sender_id", "recipient_id", "channel_index", "text",
    "rx_time", "rssi", "snr", "hop_limit", "hop_start", "relay_node",
]
WAYPOINT_FIELDS = [
    "packet_id", "sender_id", "recipient_id", "channel_index", "waypoint_id", "name",
    "description", "latitude", "longitude", "icon", "expire_at", "rx_time",
]


def _text(**overrides):
    fields = dict(
        packet_id=101, sender_id="!1fa065f0", recipient_id="^all", channel_index=1, text="hello mesh",
        rx_time=1790455379, rssi=-80, snr=5.5, hop_limit=3, hop_start=3, relay_node=240,
    )
    fields.update(overrides)
    return ReceivedTextEvent(**fields)


def _waypoint(**overrides):
    fields = dict(
        packet_id=102, sender_id="!1fa065f0", recipient_id="^all", channel_index=1, waypoint_id=4242,
        name="Cafe", description="meet here", latitude=50.4501, longitude=30.5234,
        icon=128205, expire_at=1790458979, rx_time=1790455379,
    )
    fields.update(overrides)
    return ReceivedWaypointEvent(**fields)


def _over_the_wire(data):
    """What the newline-delimited JSON transport actually does to a dict."""
    return json.loads(json.dumps(data))


def _assert_json_plain(value, path="event"):
    if isinstance(value, dict):
        for key, item in value.items():
            assert type(key) is str, f"{path}: non-str key {key!r}"
            _assert_json_plain(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_json_plain(item, f"{path}[{index}]")
    else:
        assert value is None or type(value) in (str, int, float, bool), f"{path}: {type(value).__name__}"


# --- round trips ----------------------------------------------------------


@pytest.mark.parametrize("event", [
    _text(),
    _text(rx_time=None, rssi=None, snr=None, hop_limit=None, hop_start=None, relay_node=None),
    _text(recipient_id="!756f9960", text="direct", snr=-7),
    _text(text="ünïcode ✓ \n newline", packet_id=0, channel_index=0),
])
def test_text_round_trip(event):
    restored = ipc_protocol.received_text_from_dict(_over_the_wire(ipc_protocol.received_text_to_dict(event)))

    assert restored == event
    assert type(restored) is ReceivedTextEvent


@pytest.mark.parametrize("event", [
    _waypoint(),
    _waypoint(icon=None, expire_at=None, rx_time=None),
    _waypoint(latitude=-33.8688, longitude=151.2093, name="", description=""),
    _waypoint(latitude=0, longitude=0),
])
def test_waypoint_round_trip(event):
    restored = ipc_protocol.received_waypoint_from_dict(
        _over_the_wire(ipc_protocol.received_waypoint_to_dict(event))
    )

    assert restored == event
    assert type(restored.latitude) is float


def test_envelope_dispatches_on_kind():
    for event, kind in ((_text(), "text"), (_waypoint(), "waypoint")):
        data = _over_the_wire(ipc_protocol.received_event_to_dict(event))

        assert data["kind"] == kind
        assert ipc_protocol.received_event_from_dict(data) == event


@pytest.mark.parametrize("bad", [
    {"kind": "telemetry"}, {"kind": None}, {"kind": ""}, {}, {"text": "no kind"}, None, "text", [],
])
def test_envelope_rejects_an_unknown_or_missing_kind(bad):
    with pytest.raises(ValueError):
        ipc_protocol.received_event_from_dict(bad)


def test_envelope_refuses_to_serialize_anything_that_is_not_a_received_event():
    for not_an_event in ({"kind": "text"}, "hello", None, ConnectionInfo(ConnectionState.CONNECTED, None, None)):
        with pytest.raises(TypeError):
            ipc_protocol.received_event_to_dict(not_an_event)


def test_missing_required_fields_are_loud_not_defaulted():
    data = ipc_protocol.received_text_to_dict(_text())
    del data["text"]

    with pytest.raises(KeyError):
        ipc_protocol.received_text_from_dict(data)


# --- the wire shape is exactly the whitelist, plain JSON types only --------


def test_the_events_have_exactly_the_documented_fields():
    """Adding a field (say `raw`) to a model must be a deliberate edit that
    fails here and drags the serializer and this list along with it."""
    assert [f.name for f in dataclasses.fields(ReceivedTextEvent)] == TEXT_FIELDS
    assert [f.name for f in dataclasses.fields(ReceivedWaypointEvent)] == WAYPOINT_FIELDS


def test_serialized_keys_are_exactly_kind_plus_the_fields():
    assert set(ipc_protocol.received_text_to_dict(_text())) == {"kind", *TEXT_FIELDS}
    assert set(ipc_protocol.received_waypoint_to_dict(_waypoint())) == {"kind", *WAYPOINT_FIELDS}


@pytest.mark.parametrize("event", [_text(), _waypoint(), _text(snr=None, rssi=None), _waypoint(icon=None)])
def test_serialized_events_contain_only_plain_json_types(event):
    data = ipc_protocol.received_event_to_dict(event)

    _assert_json_plain(data)
    json.dumps(data)  # and it really is JSON-encodable, no default= hook needed


# --- nothing raw / protobuf / bytes can be carried through -----------------


class _FakeProtobuf:
    """Looks like the library's MeshPacket: has a DESCRIPTOR, is not a plain type."""
    DESCRIPTOR = object()


@pytest.mark.parametrize("field,smuggled", [
    ("text", b"hello mesh"),                 # decoded.payload is bytes
    ("text", bytearray(b"hello")),
    ("sender_id", b"!1fa065f0"),
    ("recipient_id", _FakeProtobuf()),
    ("packet_id", _FakeProtobuf()),          # the raw MeshPacket where an int belongs
    ("channel_index", "1"),                  # a str is not an int
    ("packet_id", True),                     # neither is a bool
    ("rssi", b"\x00"),
    ("snr", "5.5"),
    ("rx_time", 1.5),
])
def test_a_text_event_rejects_non_plain_or_wrongly_typed_fields(field, smuggled):
    with pytest.raises(TypeError):
        _text(**{field: smuggled})


@pytest.mark.parametrize("field,smuggled", [
    ("name", b"Cafe"),
    ("description", _FakeProtobuf()),
    ("latitude", "50.4"),
    ("longitude", b"30.5"),
    ("waypoint_id", 4242.0),
    ("icon", _FakeProtobuf()),
    ("expire_at", "soon"),
])
def test_a_waypoint_event_rejects_non_plain_or_wrongly_typed_fields(field, smuggled):
    with pytest.raises(TypeError):
        _waypoint(**{field: smuggled})


def test_from_dict_rejects_bytes_and_protobuf_values_from_a_misbehaving_adapter():
    data = ipc_protocol.received_text_to_dict(_text())
    for smuggled in (b"raw bytes", _FakeProtobuf()):
        with pytest.raises(TypeError):
            ipc_protocol.received_text_from_dict({**data, "text": smuggled})
    wp = ipc_protocol.received_waypoint_to_dict(_waypoint())
    with pytest.raises(TypeError):
        ipc_protocol.received_waypoint_from_dict({**wp, "name": b"raw"})


def test_extra_keys_in_incoming_data_are_dropped_not_adopted():
    """The real packet has `raw`, `decoded.payload`, `decoded.waypoint.raw`. If
    an adapter ever leaves them in the dict, they must not become attributes."""
    text = {**ipc_protocol.received_text_to_dict(_text()),
            "raw": "protobuf-ish", "payload": "aGk=", "decoded": {"payload": "aGk="}, "fromId": None}
    waypoint = {**ipc_protocol.received_waypoint_to_dict(_waypoint()), "raw": 'id: 4242\nname: "Cafe"\n'}

    for event in (ipc_protocol.received_text_from_dict(text), ipc_protocol.received_waypoint_from_dict(waypoint)):
        assert set(vars(event)) == {f.name for f in dataclasses.fields(event)}
        for name in ("raw", "payload", "decoded", "fromId"):
            assert not hasattr(event, name), name


def test_extra_attributes_on_an_event_never_reach_the_serializer():
    """A subclass carrying `raw` (a careless adapter-side convenience) is
    serialized through the whitelist: the extra field simply isn't emitted."""

    @dataclasses.dataclass(frozen=True)
    class _LeakyText(ReceivedTextEvent):
        raw: object = b"protobuf bytes"
        payload: object = b"payload"

    leaky = _LeakyText(
        packet_id=1, sender_id="!1fa065f0", recipient_id="^all", channel_index=0, text="hi",
    )

    data = ipc_protocol.received_event_to_dict(leaky)

    assert "raw" not in data and "payload" not in data
    assert set(data) == {"kind", *TEXT_FIELDS}
    json.dumps(data)


def test_the_events_are_frozen():
    event = _text()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.text = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.raw = b"nope"  # even a brand-new attribute cannot be attached


# --- drain_received: optional, UNSUPPORTED by default ----------------------


class _MinimalTransport(RadioTransport):
    """Implements only the abstract methods - proves drain_received is NOT one
    of them, i.e. adding it did not break any existing transport or test double."""

    def connect(self, descriptor, *, force=False, timeout=30.0): ...
    def disconnect(self, *, timeout=15.0): ...
    def reconnect(self, *, timeout=30.0): ...
    def is_connected(self): return False
    def send_text(self, message, *, timeout=15.0): ...
    def send_text_checked(self, message, *, timeout=15.0): ...
    def send_packet(self, payload, destination_id, *, port_num, want_ack=False, timeout=15.0): ...
    def send_messages(self, messages, *, timeout=30.0): ...
    def send_waypoint(self, waypoint, *, timeout=15.0): ...
    def get_nodes(self, *, timeout=15.0): ...
    def get_local_node(self, *, timeout=15.0): ...
    def get_channels(self, *, timeout=15.0): ...
    def get_metadata(self, *, timeout=15.0): ...
    def set_device_time(self, epoch_seconds, *, timeout=15.0): ...
    def get_connection_info(self): ...
    def close(self): ...


def test_drain_received_is_not_abstract():
    assert "drain_received" not in RadioTransport.__abstractmethods__
    _MinimalTransport()  # instantiable without implementing it


def test_drain_received_defaults_to_unsupported_not_to_an_empty_list():
    """'cannot receive' and 'nothing received yet' must be distinguishable."""
    with pytest.raises(TransportError) as excinfo:
        _MinimalTransport().drain_received()

    assert excinfo.value.code == TransportErrorCode.UNSUPPORTED
    assert "_MinimalTransport" in excinfo.value.message


def test_drain_received_signature_is_bounded():
    with pytest.raises(TransportError):
        _MinimalTransport().drain_received(max_events=10, timeout=1.0)
