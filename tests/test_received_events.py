"""ReceivedTextEvent / ReceivedWaypointEvent / ReceivedBatch, their IPC
serialization, and the optional RadioTransport.drain_received().

Field set and envelope follow the TCP-inbound plan (sections 21-27). The
library's inbound packet dict carries protobuf/bytes fields (`raw`,
`decoded.payload`, and a second `raw` inside `decoded.waypoint`) that must never
cross the IPC boundary. These tests pin that: the events accept only plain
types, the serializer is an explicit whitelist, nothing else can ride along -
and an event cannot exist without the radio it came from
(`local_radio_node_id`), the field Core's stale-profile check depends on.
"""
import dataclasses
import json

import pytest

from meshsrv import ipc_protocol
from meshsrv.radio_transport import (
    ConnectionInfo,
    ConnectionState,
    RadioTransport,
    ReceivedBatch,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
    TransportError,
    TransportErrorCode,
)

TEXT_FIELDS = [
    "from_node_id", "to_node_id", "text", "received_at", "local_radio_node_id",
    "packet_id", "from_num", "to_num", "channel_index", "reply_id", "rx_time",
    "rx_rssi", "rx_snr", "hop_limit", "hop_start", "relay_node",
]
WAYPOINT_FIELDS = [
    "waypoint_id", "sender_id", "name", "description", "received_at", "local_radio_node_id",
    "packet_id", "latitude", "longitude", "icon", "expire_at", "channel_index",
]
LOCAL = "!756f9960"


def _text(**overrides):
    fields = dict(
        from_node_id="!1fa065f0", to_node_id="^all", text="hello mesh", received_at=1790455379.25,
        local_radio_node_id=LOCAL, packet_id=101, from_num=0x1FA065F0, to_num=0xFFFFFFFF,
        channel_index=1, reply_id=77, rx_time=1790455378, rx_rssi=-80, rx_snr=5.5,
        hop_limit=3, hop_start=3, relay_node=240,
    )
    fields.update(overrides)
    return ReceivedTextEvent(**fields)


def _waypoint(**overrides):
    fields = dict(
        waypoint_id=4242, sender_id="!1fa065f0", name="Cafe", description="meet here",
        received_at=1790455379.25, local_radio_node_id=LOCAL, packet_id=102,
        latitude=50.4501, longitude=30.5234, icon=128205, expire_at=1790458979, channel_index=1,
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
    _text(packet_id=None, from_num=None, to_num=None, channel_index=None, reply_id=None, rx_time=None,
          rx_rssi=None, rx_snr=None, hop_limit=None, hop_start=None, relay_node=None),
    _text(to_node_id="!756f9960", text="direct", rx_snr=-7),
    _text(text="Привет TCP 👋 ünïcode\n newline", packet_id=0, channel_index=0),
])
def test_text_round_trip(event):
    restored = ipc_protocol.received_text_from_dict(_over_the_wire(ipc_protocol.received_text_to_dict(event)))

    assert restored == event
    assert type(restored) is ReceivedTextEvent


@pytest.mark.parametrize("event", [
    _waypoint(),
    _waypoint(packet_id=None, icon=None, expire_at=None, channel_index=None),
    _waypoint(latitude=-33.8688, longitude=151.2093, name="", description=""),
    _waypoint(latitude=0, longitude=0),
    # a remote "delete" (expire=0, no coordinates) is representable, even though
    # ingesting it is out of scope for the first inbound PR (plan sections 50-51)
    _waypoint(latitude=None, longitude=None, expire_at=0),
])
def test_waypoint_round_trip(event):
    restored = ipc_protocol.received_waypoint_from_dict(
        _over_the_wire(ipc_protocol.received_waypoint_to_dict(event))
    )

    assert restored == event


def test_envelope_is_kind_plus_a_nested_object_named_after_the_kind():
    text = ipc_protocol.received_event_to_dict(_text())
    waypoint = ipc_protocol.received_event_to_dict(_waypoint())

    assert set(text) == {"kind", "text"} and text["kind"] == "text"
    assert set(waypoint) == {"kind", "waypoint"} and waypoint["kind"] == "waypoint"
    assert set(text["text"]) == set(TEXT_FIELDS)
    assert set(waypoint["waypoint"]) == set(WAYPOINT_FIELDS)


def test_envelope_dispatches_on_kind():
    for event in (_text(), _waypoint()):
        data = _over_the_wire(ipc_protocol.received_event_to_dict(event))

        assert ipc_protocol.received_event_from_dict(data) == event


@pytest.mark.parametrize("bad", [
    {"kind": "telemetry"}, {"kind": None}, {"kind": ""}, {}, {"text": {"text": "no kind"}}, None, "text", [],
    {"kind": "text"},                                   # no body
    {"kind": "text", "text": "not an object"},
    {"kind": "waypoint", "text": {"text": "wrong key"}},  # body under the wrong name
])
def test_envelope_rejects_an_unknown_missing_or_bodyless_kind(bad):
    with pytest.raises(ValueError):
        ipc_protocol.received_event_from_dict(bad)


def test_envelope_refuses_to_serialize_anything_that_is_not_a_received_event():
    for not_an_event in ({"kind": "text"}, "hello", None, ConnectionInfo(ConnectionState.CONNECTED, None, None)):
        with pytest.raises(TypeError):
            ipc_protocol.received_event_to_dict(not_an_event)


def test_missing_required_fields_are_loud_not_defaulted():
    for missing in ("text", "from_node_id", "to_node_id", "received_at", "local_radio_node_id"):
        data = ipc_protocol.received_text_to_dict(_text())
        del data[missing]
        with pytest.raises(KeyError):
            ipc_protocol.received_text_from_dict(data)
    for missing in ("waypoint_id", "sender_id", "name", "received_at", "local_radio_node_id"):
        data = ipc_protocol.received_waypoint_to_dict(_waypoint())
        del data[missing]
        with pytest.raises(KeyError):
            ipc_protocol.received_waypoint_from_dict(data)


# --- the event always says which radio it came from ------------------------


def test_local_radio_node_id_is_required_and_non_empty():
    with pytest.raises(TypeError):
        ReceivedTextEvent(from_node_id="!1", to_node_id="^all", text="x", received_at=1.0)
    with pytest.raises(TypeError):
        ReceivedWaypointEvent(waypoint_id=1, sender_id="!1", name="", description="", received_at=1.0)
    for empty_or_wrong in ("", None, 0x756F9960, b"!756f9960"):
        with pytest.raises((ValueError, TypeError)):
            _text(local_radio_node_id=empty_or_wrong)
        with pytest.raises((ValueError, TypeError)):
            _waypoint(local_radio_node_id=empty_or_wrong)


def test_local_radio_node_id_survives_the_wire():
    assert ipc_protocol.received_event_from_dict(
        _over_the_wire(ipc_protocol.received_event_to_dict(_text()))
    ).local_radio_node_id == LOCAL
    assert ipc_protocol.received_event_from_dict(
        _over_the_wire(ipc_protocol.received_event_to_dict(_waypoint()))
    ).local_radio_node_id == LOCAL


# --- the wire shape is exactly the whitelist, plain JSON types only --------


def test_the_events_have_exactly_the_documented_fields():
    """Adding a field (say `raw`) to a model must be a deliberate edit that
    fails here and drags the serializer and this list along with it."""
    assert [f.name for f in dataclasses.fields(ReceivedTextEvent)] == TEXT_FIELDS
    assert [f.name for f in dataclasses.fields(ReceivedWaypointEvent)] == WAYPOINT_FIELDS
    assert [f.name for f in dataclasses.fields(ReceivedBatch)] == [
        "events", "dropped", "malformed", "connection_generation"
    ]


@pytest.mark.parametrize("event", [_text(), _waypoint(), _text(rx_snr=None, rx_rssi=None), _waypoint(icon=None)])
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
    ("from_node_id", b"!1fa065f0"),
    ("to_node_id", _FakeProtobuf()),
    ("local_radio_node_id", b"!756f9960"),
    ("packet_id", _FakeProtobuf()),          # the raw MeshPacket where an int belongs
    ("channel_index", "1"),                  # a str is not an int
    ("packet_id", True),                     # neither is a bool
    ("rx_rssi", b"\x00"),
    ("rx_snr", "5.5"),
    ("received_at", "now"),
    ("rx_time", 1.5),
    ("reply_id", b"\x01"),
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
    ("sender_id", b"!1fa065f0"),
    ("received_at", None),
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

    leaky = _LeakyText(from_node_id="!1fa065f0", to_node_id="^all", text="hi", received_at=1.0,
                       local_radio_node_id=LOCAL)

    data = ipc_protocol.received_event_to_dict(leaky)

    assert set(data["text"]) == set(TEXT_FIELDS)
    assert "raw" not in data["text"] and "payload" not in data["text"]
    json.dumps(data)


def test_the_events_are_frozen():
    event = _text()
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.text = "changed"
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.raw = b"nope"  # even a brand-new attribute cannot be attached


# --- batches: overflow and malformed events are counted, never silent -------


def test_batch_round_trip_preserves_order_and_counters():
    batch = ReceivedBatch(
        events=(_text(packet_id=1), _waypoint(), _text(packet_id=3)), dropped=7, malformed=2,
        connection_generation=4,
    )

    restored = ipc_protocol.received_batch_from_dict(_over_the_wire(ipc_protocol.received_batch_to_dict(batch)))

    assert restored == batch
    assert [type(e).__name__ for e in restored.events] == ["ReceivedTextEvent", "ReceivedWaypointEvent", "ReceivedTextEvent"]
    assert (restored.dropped, restored.malformed, restored.connection_generation) == (7, 2, 4)


def test_an_empty_batch_is_valid_and_distinct_from_unsupported():
    restored = ipc_protocol.received_batch_from_dict({"events": []})

    assert restored == ReceivedBatch()
    assert restored.events == () and restored.dropped == 0 and restored.malformed == 0


def test_one_malformed_event_is_discarded_and_counted_not_the_whole_batch():
    good_text = ipc_protocol.received_event_to_dict(_text(packet_id=1))
    good_waypoint = ipc_protocol.received_event_to_dict(_waypoint())
    bad = [
        {"kind": "text", "text": {**good_text["text"], "text": b"raw bytes"}},      # bytes smuggled
        {"kind": "text", "text": {k: v for k, v in good_text["text"].items() if k != "local_radio_node_id"}},
        {"kind": "text", "text": {**good_text["text"], "local_radio_node_id": ""}},  # unattributable
        {"kind": "telemetry"},
        "garbage",
        None,
    ]
    # not run through JSON: a misbehaving adapter's bytes value cannot be JSON-encoded, which is the point
    payload = {"events": [good_text, *bad, good_waypoint], "dropped": 3, "malformed": 1}

    batch = ipc_protocol.received_batch_from_dict(payload)

    assert [type(e).__name__ for e in batch.events] == ["ReceivedTextEvent", "ReceivedWaypointEvent"]
    assert batch.malformed == 1 + len(bad)  # the adapter's own count plus what failed here
    assert batch.dropped == 3


@pytest.mark.parametrize("bad", [None, [], "events", {}, {"events": None}, {"events": "x"}, {"events": {}}])
def test_a_batch_that_is_not_an_object_with_an_events_list_is_rejected(bad):
    with pytest.raises(ValueError):
        ipc_protocol.received_batch_from_dict(bad)


def test_a_batch_only_holds_received_events():
    for not_an_event in ("hello", {"kind": "text"}, None, b"raw", ConnectionInfo(ConnectionState.CONNECTED, None, None)):
        with pytest.raises(TypeError):
            ReceivedBatch(events=(not_an_event,))
    with pytest.raises(TypeError):
        ReceivedBatch(dropped="3")


def test_batch_serialization_is_plain_json():
    data = ipc_protocol.received_batch_to_dict(ReceivedBatch(events=(_text(), _waypoint()), dropped=1))

    _assert_json_plain(data, "batch")
    json.dumps(data)


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


def test_drain_received_defaults_to_unsupported_not_to_an_empty_batch():
    """'cannot receive' and 'nothing received yet' must be distinguishable."""
    with pytest.raises(TransportError) as excinfo:
        _MinimalTransport().drain_received()

    assert excinfo.value.code == TransportErrorCode.UNSUPPORTED
    assert "_MinimalTransport" in excinfo.value.message


def test_drain_received_takes_a_bounded_limit_and_timeout():
    with pytest.raises(TransportError):
        _MinimalTransport().drain_received(limit=50, timeout=1.0)
