"""ReceivedNodeInfoEvent / ReceivedPositionEvent / ReceivedTelemetryEvent, their
IPC serialization, and their place in ReceivedBatch.

Same contract as text/waypoint (see test_received_events.py): frozen, plain
types only, `local_radio_node_id` mandatory, explicit per-field (de)serializers,
never `asdict()`. Field choices and the library's own quirks (enum names as
strings, proto3 default-omission, Position's pre-converted floats, Telemetry's
oneof) are confirmed against the real library in
adapters/meshtastic/verify_receive_topics.py, not assumed from reading source.
"""
import dataclasses
import json

import pytest

from meshsrv import ipc_protocol
from meshsrv.radio_transport import (
    ReceivedBatch,
    ReceivedNodeInfoEvent,
    ReceivedPositionEvent,
    ReceivedTelemetryEvent,
    ReceivedTextEvent,
    TELEMETRY_KINDS,
)

LOCAL = "!756f9960"

NODEINFO_FIELDS = [
    "node_id", "sender_id", "received_at", "local_radio_node_id", "packet_id", "long_name",
    "short_name", "hw_model", "role", "is_licensed", "channel_index", "rx_time", "rx_rssi",
    "rx_snr", "hop_limit", "hop_start", "relay_node",
]
POSITION_FIELDS = [
    "sender_id", "received_at", "local_radio_node_id", "packet_id", "latitude", "longitude",
    "altitude", "ground_speed", "sats_in_view", "position_time", "channel_index", "rx_time",
    "rx_rssi", "rx_snr", "hop_limit", "hop_start", "relay_node",
]
TELEMETRY_FIELDS = [
    "sender_id", "kind", "metrics", "received_at", "local_radio_node_id", "packet_id",
    "telemetry_time", "channel_index", "rx_time", "rx_rssi", "rx_snr", "hop_limit", "hop_start",
    "relay_node",
]


def _nodeinfo(**overrides):
    fields = dict(
        node_id="!1fa065f0", sender_id="!1fa065f0", received_at=1790455379.25, local_radio_node_id=LOCAL,
        packet_id=103, long_name="Test Node", short_name="TST", hw_model="RAK4631", role="ROUTER",
        is_licensed=True, channel_index=1, rx_time=1790455378, rx_rssi=-80, rx_snr=5.5, hop_limit=3,
        hop_start=3, relay_node=240,
    )
    fields.update(overrides)
    return ReceivedNodeInfoEvent(**fields)


def _position(**overrides):
    fields = dict(
        sender_id="!1fa065f0", received_at=1790455379.25, local_radio_node_id=LOCAL, packet_id=104,
        latitude=50.4501, longitude=30.5234, altitude=123, ground_speed=5, sats_in_view=8,
        position_time=1790000000, channel_index=1, rx_time=1790455378, rx_rssi=-80, rx_snr=5.5,
        hop_limit=3, hop_start=3, relay_node=240,
    )
    fields.update(overrides)
    return ReceivedPositionEvent(**fields)


def _telemetry(**overrides):
    fields = dict(
        sender_id="!1fa065f0", kind="device",
        metrics={"batteryLevel": 80, "voltage": 3.9, "channelUtilization": 1.2, "airUtilTx": 0.5, "uptimeSeconds": 1000},
        received_at=1790455379.25, local_radio_node_id=LOCAL, packet_id=105, telemetry_time=1790000000,
        channel_index=1, rx_time=1790455378, rx_rssi=-80, rx_snr=5.5, hop_limit=3, hop_start=3, relay_node=240,
    )
    fields.update(overrides)
    return ReceivedTelemetryEvent(**fields)


def _over_the_wire(data):
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


class _FakeProtobuf:
    DESCRIPTOR = object()


# --- field lists pinned, matching the whitelist exactly ---------------------


def test_the_events_have_exactly_the_documented_fields():
    assert [f.name for f in dataclasses.fields(ReceivedNodeInfoEvent)] == NODEINFO_FIELDS
    assert [f.name for f in dataclasses.fields(ReceivedPositionEvent)] == POSITION_FIELDS
    assert [f.name for f in dataclasses.fields(ReceivedTelemetryEvent)] == TELEMETRY_FIELDS


def test_macaddr_and_public_key_are_not_carried():
    """Deliberate scope decision (see ReceivedNodeInfoEvent's own docstring):
    neither is needed by anything Core does with a NodeInfo today."""
    assert "macaddr" not in NODEINFO_FIELDS and "public_key" not in NODEINFO_FIELDS


# --- round trips --------------------------------------------------------------


@pytest.mark.parametrize("event", [
    _nodeinfo(),
    _nodeinfo(long_name=None, short_name=None, hw_model=None, role=None, is_licensed=None,
              rx_time=None, rx_rssi=None, rx_snr=None, hop_limit=None, hop_start=None, relay_node=None),
    _nodeinfo(is_licensed=False),  # a real, present value - not "absent" - must survive
    _nodeinfo(long_name="Привет 👋", hw_model="TBEAM"),
])
def test_nodeinfo_round_trip(event):
    restored = ipc_protocol.received_nodeinfo_from_dict(_over_the_wire(ipc_protocol.received_nodeinfo_to_dict(event)))

    assert restored == event and type(restored) is ReceivedNodeInfoEvent


@pytest.mark.parametrize("event", [
    _position(),
    _position(altitude=None, ground_speed=None, sats_in_view=None, position_time=None,
              rx_time=None, rx_rssi=None, rx_snr=None, hop_limit=None, hop_start=None, relay_node=None),
    _position(latitude=-33.8688, longitude=151.2093, altitude=-5),
    _position(latitude=0, longitude=0),
])
def test_position_round_trip(event):
    restored = ipc_protocol.received_position_from_dict(_over_the_wire(ipc_protocol.received_position_to_dict(event)))

    assert restored == event


@pytest.mark.parametrize("event", [
    _telemetry(),
    _telemetry(kind="environment", metrics={"temperature": 21.5, "relativeHumidity": 40.0}),
    _telemetry(kind="power", metrics={"ch1Voltage": 5.0, "ch1Current": 0.5}),
    _telemetry(metrics={}),  # an empty metrics dict is still representable
    _telemetry(telemetry_time=None, rx_time=None, rx_snr=None),
])
def test_telemetry_round_trip(event):
    restored = ipc_protocol.received_telemetry_from_dict(_over_the_wire(ipc_protocol.received_telemetry_to_dict(event)))

    assert restored == event
    assert restored.metrics == event.metrics and restored.metrics is not event.metrics  # a real copy, not aliased


def test_envelope_kinds_and_dispatch():
    for event, kind in (
        (_nodeinfo(), "nodeinfo"), (_position(), "position"), (_telemetry(), "telemetry"),
    ):
        data = _over_the_wire(ipc_protocol.received_event_to_dict(event))

        assert data["kind"] == kind and set(data) == {"kind", kind}
        assert ipc_protocol.received_event_from_dict(data) == event


def test_missing_required_fields_are_loud():
    for missing in ("node_id", "sender_id", "received_at", "local_radio_node_id"):
        data = ipc_protocol.received_nodeinfo_to_dict(_nodeinfo())
        del data[missing]
        with pytest.raises(KeyError):
            ipc_protocol.received_nodeinfo_from_dict(data)
    for missing in ("sender_id", "received_at", "local_radio_node_id"):
        data = ipc_protocol.received_position_to_dict(_position())
        del data[missing]
        with pytest.raises(KeyError):
            ipc_protocol.received_position_from_dict(data)
    for missing in ("sender_id", "kind", "metrics", "received_at", "local_radio_node_id"):
        data = ipc_protocol.received_telemetry_to_dict(_telemetry())
        del data[missing]
        with pytest.raises(KeyError):
            ipc_protocol.received_telemetry_from_dict(data)


# --- local_radio_node_id is still mandatory everywhere -----------------------


def test_local_radio_node_id_is_required_and_non_empty_for_all_three():
    with pytest.raises(TypeError):
        ReceivedNodeInfoEvent(node_id="!1", sender_id="!1", received_at=1.0)
    with pytest.raises(TypeError):
        ReceivedPositionEvent(sender_id="!1", received_at=1.0)
    with pytest.raises(TypeError):
        ReceivedTelemetryEvent(sender_id="!1", kind="device", metrics={}, received_at=1.0)
    for empty_or_wrong in ("", None, 0x756F9960, b"!756f9960"):
        with pytest.raises((ValueError, TypeError)):
            _nodeinfo(local_radio_node_id=empty_or_wrong)
        with pytest.raises((ValueError, TypeError)):
            _position(local_radio_node_id=empty_or_wrong)
        with pytest.raises((ValueError, TypeError)):
            _telemetry(local_radio_node_id=empty_or_wrong)


# --- telemetry's own kind/metrics validation ----------------------------------


def test_telemetry_kind_must_be_a_known_variant():
    for bad_kind in ("battery", "Device", "", None, 1, "device "):
        with pytest.raises((ValueError, TypeError)):
            _telemetry(kind=bad_kind)
    for good_kind in TELEMETRY_KINDS:
        _telemetry(kind=good_kind)  # does not raise


@pytest.mark.parametrize("bad_metrics", [
    "not a dict", None, [], {1: 2.0}, {"battery": "80"}, {"battery": True}, {"battery": b"80"},
    {"battery": {"nested": 1}}, {"battery": _FakeProtobuf()},
])
def test_telemetry_metrics_must_be_a_flat_dict_of_plain_numbers(bad_metrics):
    with pytest.raises((TypeError, KeyError)):
        _telemetry(metrics=bad_metrics)


def test_telemetry_metrics_is_not_shared_with_the_caller():
    """The stored dict must be a defensive copy, not an alias the caller could
    still mutate after construction."""
    source = {"batteryLevel": 80}
    event = _telemetry(metrics=source)
    source["batteryLevel"] = 999

    assert event.metrics == {"batteryLevel": 80}


# --- is_licensed: a real bool, not an int/str standing in for one -----------


@pytest.mark.parametrize("bad", [1, 0, "true", "False", b"\x01"])
def test_is_licensed_rejects_non_bool(bad):
    with pytest.raises(TypeError):
        _nodeinfo(is_licensed=bad)


def test_is_licensed_false_is_a_real_value_not_absence():
    event = _nodeinfo(is_licensed=False)

    assert event.is_licensed is False
    data = ipc_protocol.received_nodeinfo_to_dict(event)
    assert data["is_licensed"] is False  # present, not omitted like the library's own default-value dict would


# --- the wire shape is exactly the whitelist, plain JSON types only ---------


@pytest.mark.parametrize("event", [_nodeinfo(), _position(), _telemetry()])
def test_serialized_events_contain_only_plain_json_types(event):
    data = ipc_protocol.received_event_to_dict(event)

    _assert_json_plain(data)
    json.dumps(data)


# --- nothing raw / protobuf / bytes / enum object can be carried through ----


@pytest.mark.parametrize("field,smuggled", [
    ("node_id", b"!1fa065f0"), ("sender_id", _FakeProtobuf()), ("long_name", b"name"),
    ("hw_model", _FakeProtobuf()), ("role", 2),  # the raw enum int, not its name string
    ("packet_id", "103"), ("rx_snr", "5.5"), ("received_at", None),
])
def test_a_nodeinfo_event_rejects_non_plain_or_wrongly_typed_fields(field, smuggled):
    with pytest.raises(TypeError):
        _nodeinfo(**{field: smuggled})


@pytest.mark.parametrize("field,smuggled", [
    ("sender_id", b"!1fa065f0"), ("latitude", "50.4"), ("altitude", 1.5),
    ("ground_speed", _FakeProtobuf()), ("received_at", "now"),
])
def test_a_position_event_rejects_non_plain_or_wrongly_typed_fields(field, smuggled):
    with pytest.raises(TypeError):
        _position(**{field: smuggled})


def test_from_dict_rejects_bytes_and_protobuf_from_a_misbehaving_adapter():
    data = ipc_protocol.received_nodeinfo_to_dict(_nodeinfo())
    with pytest.raises(TypeError):
        ipc_protocol.received_nodeinfo_from_dict({**data, "long_name": b"raw"})
    data = ipc_protocol.received_position_to_dict(_position())
    with pytest.raises(TypeError):
        ipc_protocol.received_position_from_dict({**data, "latitude": _FakeProtobuf()})
    data = ipc_protocol.received_telemetry_to_dict(_telemetry())
    with pytest.raises(TypeError):
        ipc_protocol.received_telemetry_from_dict({**data, "metrics": {"battery": _FakeProtobuf()}})


def test_extra_keys_in_incoming_data_are_dropped_not_adopted():
    """The real packet also has `raw`, `decoded.payload`, and (User) `macaddr`
    / `publicKey` we deliberately never asked for - none may become attributes."""
    data = {**ipc_protocol.received_nodeinfo_to_dict(_nodeinfo()),
            "raw": "protobuf-ish", "macaddr": "AQIDBAUG", "publicKey": "aa==", "fromId": None}

    event = ipc_protocol.received_nodeinfo_from_dict(data)

    assert set(vars(event)) == {f.name for f in dataclasses.fields(event)}
    for name in ("raw", "macaddr", "publicKey", "fromId"):
        assert not hasattr(event, name)


def test_extra_attributes_on_an_event_never_reach_the_serializer():
    """A subclass carrying `raw` (a careless adapter-side convenience) is
    serialized through the whitelist: the extra field simply isn't emitted -
    the same guarantee test_received_events.py pins for text/waypoint."""

    @dataclasses.dataclass(frozen=True)
    class _LeakyNodeInfo(ReceivedNodeInfoEvent):
        raw: object = b"protobuf bytes"
        macaddr: object = "AQIDBAUG"

    leaky = _LeakyNodeInfo(node_id="!1fa065f0", sender_id="!1fa065f0", received_at=1.0, local_radio_node_id=LOCAL)

    data = ipc_protocol.received_event_to_dict(leaky)

    assert set(data["nodeinfo"]) == set(NODEINFO_FIELDS)
    assert "raw" not in data["nodeinfo"] and "macaddr" not in data["nodeinfo"]
    json.dumps(data)


def test_the_events_are_frozen():
    for event in (_nodeinfo(), _position(), _telemetry()):
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.received_at = 0.0
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.raw = b"nope"


# --- batches hold all five event kinds together -------------------------------


def test_a_batch_can_mix_all_five_kinds_in_order():
    from test_received_events import _text, _waypoint  # sibling module's builders

    batch = ReceivedBatch(events=(_text(), _waypoint(), _nodeinfo(), _position(), _telemetry()))

    restored = ipc_protocol.received_batch_from_dict(_over_the_wire(ipc_protocol.received_batch_to_dict(batch)))

    assert restored == batch
    assert [type(e).__name__ for e in restored.events] == [
        "ReceivedTextEvent", "ReceivedWaypointEvent", "ReceivedNodeInfoEvent",
        "ReceivedPositionEvent", "ReceivedTelemetryEvent",
    ]


def test_a_batch_rejects_the_new_types_when_not_constructed_through_the_dataclass():
    with pytest.raises(TypeError):
        ReceivedBatch(events=({"kind": "nodeinfo"},))
