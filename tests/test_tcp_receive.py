"""TCP inbound, adapter side: pubsub capture -> explicit normalization -> bounded
queue -> drain_received() over IPC and through the router. Not wired to Core.

Runs against a fake pubsub and a fake TCPInterface (no network, no real
`meshtastic`); the real-library end to end is in
tests/test_tcp_transport_integration.py. Covers the plan's sections 64-70:
normalization without raw/bytes/protobuf, the interface filter, overflow with
its dropped counter, queue survival across a same-radio reconnect, and an
asynchronous pubsub event not disturbing the request/response IPC.
"""
import io
import json
import sys
import threading
import time
import types

import pytest

from adapters.meshtastic import tcp_transport as tcp_mod
from adapters.meshtastic.ipc_server import _AdapterDispatcher, serve_forever
from adapters.meshtastic.tcp_transport import TCPTransport
from meshsrv import ipc_protocol
from meshsrv.adapter_ipc_client import AdapterIPCTransport
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    ReceivedBatch,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
    TransportError,
    TransportErrorCode,
)
from meshsrv.transport_router import TransportRouter

LOCAL_NUM = 0x756F9960
LOCAL_ID = "!756f9960"
TEXT_TOPIC = "meshtastic.receive.text"
WAYPOINT_TOPIC = "meshtastic.receive.waypoint"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakePub:
    """The bit of pypubsub the transport uses. Delivery is synchronous."""

    def __init__(self):
        self.listeners = []  # [(topic, listener)]
        self.subscribe_calls = 0
        self.unsubscribe_calls = 0

    def subscribe(self, listener, topic):
        self.subscribe_calls += 1
        self.listeners.append((topic, listener))

    def unsubscribe(self, listener, topic):
        self.unsubscribe_calls += 1
        self.listeners = [(t, l) for t, l in self.listeners if not (t == topic and l == listener)]

    def send(self, topic, packet, interface):
        for subscribed_topic, listener in list(self.listeners):
            if subscribed_topic == topic:
                listener(packet=packet, interface=interface)


class FakeReader:
    def is_alive(self):
        return True


class FakeInterface:
    """Stands in for meshtastic TCPInterface: connected, knows which radio it
    is (myInfo), records any call so a test can prove drain never touches it."""

    instances = []

    def __init__(self, hostname="", portNumber=4403, connectNow=True, my_node_num=LOCAL_NUM):
        self.myInfo = types.SimpleNamespace(my_node_num=my_node_num) if my_node_num is not None else None
        self._rxThread = FakeReader()
        self.socket = None
        self.calls = []
        self.closed = False
        FakeInterface.instances.append(self)

    def close(self):
        self.closed = True

    def sendText(self, *a, **k):
        self.calls.append("sendText")


class FakeProtobuf:
    """A MeshPacket look-alike: it must never be read, let alone carried."""
    DESCRIPTOR = object()


class RecordingDict(dict):
    """Remembers which keys the normalizer touched."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.touched = set()

    def __getitem__(self, key):
        self.touched.add(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.touched.add(key)
        return super().get(key, default)


def text_packet(**overrides):
    packet = {
        "from": 0x1FA065F0, "to": 0xFFFFFFFF, "id": 101, "channel": 1, "rxTime": 1790455378,
        "rxSnr": 5.5, "rxRssi": -80, "hopLimit": 3, "hopStart": 3, "relayNode": 240,
        "fromId": None, "toId": "^all",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"hello mesh", "text": "hello mesh"},
        "raw": FakeProtobuf(),
    }
    packet.update(overrides)
    return packet


def waypoint_packet(**waypoint_overrides):
    waypoint = {
        "id": 4242, "latitudeI": 504501000, "longitudeI": 305234000, "expire": 1790458979,
        "name": "Cafe", "description": "meet here", "icon": 128205, "raw": 'id: 4242\nname: "Cafe"\n',
    }
    waypoint.update(waypoint_overrides)
    return {
        "from": 0x1FA065F0, "to": 0xFFFFFFFF, "id": 102, "channel": 1,
        "decoded": {"portnum": "WAYPOINT_APP", "payload": b"\x08\x92\x21", "waypoint": waypoint},
        "raw": FakeProtobuf(),
    }


@pytest.fixture
def pub(monkeypatch):
    fake = FakePub()
    monkeypatch.setitem(sys.modules, "pubsub", types.SimpleNamespace(pub=fake))
    return fake


@pytest.fixture
def logs():
    return []


@pytest.fixture
def transport(pub, logs):
    """A transport with the subscription made and `interface` as its current
    interface (as after a successful connect)."""
    instance = TCPTransport(host="192.168.2.34", on_log=lambda msg, level="INFO": logs.append((level, msg)))
    instance._ensure_receive_subscription()
    instance._interface = FakeInterface()
    return instance


def deliver_text(pub, transport, packet=None, interface=None):
    pub.send(TEXT_TOPIC, packet if packet is not None else text_packet(),
             interface if interface is not None else transport._interface)


def deliver_waypoint(pub, transport, packet=None, interface=None):
    pub.send(WAYPOINT_TOPIC, packet if packet is not None else waypoint_packet(),
             interface if interface is not None else transport._interface)


# ---------------------------------------------------------------------------
# Subscription lifetime
# ---------------------------------------------------------------------------


def test_subscribes_to_exactly_the_two_receive_topics_once(pub):
    transport = TCPTransport(host="192.168.2.34")
    for _ in range(3):
        transport._ensure_receive_subscription()

    assert sorted(topic for topic, _ in pub.listeners) == [TEXT_TOPIC, WAYPOINT_TOPIC]
    assert pub.subscribe_calls == 2, "not per call, and no catch-all 'meshtastic.receive'"


@pytest.fixture
def fake_connectable(pub, monkeypatch):
    """connect()/reconnect() against a fake socket and a fake TCPInterface."""
    FakeInterface.instances = []

    class FakeSocket:
        def settimeout(self, value):
            pass

        def close(self):
            pass

    fake_module = types.ModuleType("meshtastic.tcp_interface")
    fake_module.TCPInterface = FakeInterface
    monkeypatch.setitem(sys.modules, "meshtastic.tcp_interface", fake_module)
    monkeypatch.setattr(tcp_mod.socket, "create_connection", lambda address, timeout=None: FakeSocket())
    monkeypatch.setattr(tcp_mod.time, "sleep", lambda s: None)


def _descriptor():
    return ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")


def test_connect_and_reconnect_never_stack_subscriptions(pub, fake_connectable):
    transport = TCPTransport(host="192.168.2.34")

    transport.connect(_descriptor(), timeout=5)
    transport.connect(_descriptor(), force=True, timeout=5)
    transport.reconnect(timeout=30)

    assert pub.subscribe_calls == 2
    assert len(pub.listeners) == 2


def test_close_unsubscribes_and_is_idempotent(pub, fake_connectable):
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)

    transport.close()
    transport.close()

    assert pub.listeners == []
    assert pub.unsubscribe_calls == 2


def test_a_plain_disconnect_keeps_the_subscription(pub, fake_connectable):
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)

    transport.disconnect(timeout=5)

    assert len(pub.listeners) == 2, "only close() ends the lifetime"


def test_missing_pubsub_disables_receiving_without_breaking_connect(monkeypatch, fake_connectable, logs):
    monkeypatch.setitem(sys.modules, "pubsub", None)  # `from pubsub import pub` -> ImportError
    transport = TCPTransport(host="192.168.2.34", on_log=lambda msg, level="INFO": logs.append((level, msg)))

    info = transport.connect(_descriptor(), timeout=5)

    assert info.state == ConnectionState.CONNECTED
    with pytest.raises(TransportError) as excinfo:
        transport.drain_received()
    assert excinfo.value.code == TransportErrorCode.UNSUPPORTED
    assert any("receive disabled" in message for _, message in logs)


# ---------------------------------------------------------------------------
# Normalization (plan section 64-65)
# ---------------------------------------------------------------------------


def test_text_is_normalized_to_a_neutral_event(pub, transport):
    deliver_text(pub, transport)

    batch = transport.drain_received()

    assert len(batch.events) == 1
    event = batch.events[0]
    assert type(event) is ReceivedTextEvent
    assert (event.from_node_id, event.to_node_id, event.text) == ("!1fa065f0", "^all", "hello mesh")
    assert (event.from_num, event.to_num) == (0x1FA065F0, 0xFFFFFFFF)
    assert (event.packet_id, event.channel_index, event.rx_time) == (101, 1, 1790455378)
    assert (event.rx_rssi, event.rx_snr, event.hop_limit, event.hop_start, event.relay_node) == (-80, 5.5, 3, 3, 240)
    assert event.local_radio_node_id == LOCAL_ID
    assert abs(event.received_at - time.time()) < 5


def test_the_normalizer_never_reads_raw_or_payload(pub, transport):
    packet = RecordingDict(text_packet())
    packet["decoded"] = RecordingDict(packet["decoded"])
    deliver_text(pub, transport, packet)

    assert len(transport.drain_received().events) == 1
    assert "raw" not in packet.touched
    assert "payload" not in packet["decoded"].touched


def test_a_drained_batch_serializes_to_plain_json_with_no_raw_bytes_or_protobuf(pub, transport):
    deliver_text(pub, transport)
    deliver_waypoint(pub, transport)

    batch = transport.drain_received()
    wire = json.dumps(ipc_protocol.received_batch_to_dict(batch))  # would raise on bytes / protobuf

    for forbidden in ("raw", "payload", "DESCRIPTOR", "protobuf", "b'"):
        assert forbidden not in wire, forbidden
    for event in batch.events:
        assert not any(hasattr(event, name) for name in ("raw", "payload", "decoded"))


def test_a_direct_message_keeps_its_recipient(pub, transport):
    deliver_text(pub, transport, text_packet(to=LOCAL_NUM, toId=LOCAL_ID))

    event = transport.drain_received().events[0]

    assert event.to_node_id == LOCAL_ID and event.to_num == LOCAL_NUM


def test_protobuf_defaults_the_library_omits_are_filled_in_or_none(pub, transport):
    packet = text_packet()
    for omitted in ("channel", "id", "rxTime", "rxRssi", "hopLimit", "hopStart", "relayNode", "rxSnr"):
        del packet[omitted]
    deliver_text(pub, transport, packet)

    event = transport.drain_received().events[0]

    assert event.channel_index == 0  # the primary channel is protobuf default 0, hence omitted
    assert event.packet_id is None and event.rx_time is None and event.rx_rssi is None
    assert event.hop_limit is None and event.hop_start is None and event.relay_node is None and event.rx_snr is None


def test_reply_id_comes_from_decoded(pub, transport):
    packet = text_packet()
    packet["decoded"] = {**packet["decoded"], "replyId": 77}
    deliver_text(pub, transport, packet)

    assert transport.drain_received().events[0].reply_id == 77


def test_the_sender_is_built_from_the_numeric_from_not_from_id(pub, transport):
    """`fromId` is None while the sender is not in the local NodeDB."""
    deliver_text(pub, transport, text_packet(fromId=None))
    deliver_text(pub, transport, text_packet(fromId="!deadbeef", id=102))  # a wrong fromId must not be believed either

    events = transport.drain_received().events

    assert [e.from_node_id for e in events] == ["!1fa065f0", "!1fa065f0"]


def test_waypoint_is_normalized_with_coordinates_in_degrees(pub, transport):
    packet = RecordingDict(waypoint_packet())
    deliver_waypoint(pub, transport, packet)

    event = transport.drain_received().events[0]

    assert type(event) is ReceivedWaypointEvent
    assert (event.waypoint_id, event.sender_id, event.name, event.description) == (4242, "!1fa065f0", "Cafe", "meet here")
    assert (event.latitude, event.longitude) == (pytest.approx(50.4501), pytest.approx(30.5234))
    assert (event.icon, event.expire_at, event.channel_index, event.packet_id) == (128205, 1790458979, 1, 102)
    assert event.local_radio_node_id == LOCAL_ID
    assert "raw" not in packet.touched


def test_a_waypoint_without_coordinates_is_still_representable(pub, transport):
    """The shape of a remote delete (expire 0, no position). Ingesting it is a
    later, separate decision; capturing it faithfully costs nothing."""
    packet = waypoint_packet()
    for key in ("latitudeI", "longitudeI", "expire"):
        del packet["decoded"]["waypoint"][key]
    deliver_waypoint(pub, transport, packet)

    event = transport.drain_received().events[0]

    assert event.latitude is None and event.longitude is None and event.expire_at is None


def test_empty_text_is_ignored_not_counted_as_malformed(pub, transport):
    packet = text_packet()
    packet["decoded"] = {**packet["decoded"], "text": "   "}
    deliver_text(pub, transport, packet)

    batch = transport.drain_received()

    assert batch.events == () and batch.malformed == 0


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("decoded"),
    lambda p: p["decoded"].pop("text"),
    lambda p: p["decoded"].update(text=b"bytes are not text"),
    lambda p: p.pop("from"),
    lambda p: p.pop("to"),
    lambda p: p.update({"from": "!1fa065f0"}),
    lambda p: p.update({"to": True}),
])
def test_a_malformed_text_packet_is_counted_and_costs_nothing_else(pub, transport, mutate):
    bad = text_packet()
    bad["decoded"] = dict(bad["decoded"])
    mutate(bad)
    deliver_text(pub, transport, bad)
    deliver_text(pub, transport, text_packet(id=999))  # the next, good one still arrives

    batch = transport.drain_received()

    assert [e.packet_id for e in batch.events] == [999]
    assert batch.malformed == 1
    assert transport.get_receive_stats()["malformed_events"] == 1


@pytest.mark.parametrize("mutate", [
    lambda w: w.pop("id"),
    lambda w: w.update(id="4242"),
    lambda w: w.update(name=b"Cafe"),
    lambda w: w.update(description=None),
])
def test_a_malformed_waypoint_is_counted_and_costs_nothing_else(pub, transport, mutate):
    bad = waypoint_packet()
    mutate(bad["decoded"]["waypoint"])
    deliver_waypoint(pub, transport, bad)
    deliver_waypoint(pub, transport, waypoint_packet(id=5))

    batch = transport.drain_received()

    assert [e.waypoint_id for e in batch.events] == [5]
    assert batch.malformed == 1


def test_an_event_from_an_interface_that_does_not_know_its_radio_yet_is_malformed_not_misattributed(pub, transport):
    transport._interface = FakeInterface(my_node_num=None)  # myInfo not received yet
    deliver_text(pub, transport)

    batch = transport.drain_received()

    assert batch.events == () and batch.malformed == 1, "no event may exist without local_radio_node_id"


def test_callbacks_never_raise_into_the_publishing_thread(pub, transport):
    for garbage in (None, "string", 42, [], {}, {"decoded": None}, {"decoded": {"text": None}}):
        deliver_text(pub, transport, garbage if garbage is not None else {})
    pub.send(TEXT_TOPIC, None, transport._interface)
    pub.send(WAYPOINT_TOPIC, "garbage", transport._interface)

    assert transport.drain_received().events == ()
    assert transport.get_receive_stats()["malformed_events"] >= 1


# ---------------------------------------------------------------------------
# Interface filter (plan section 66)
# ---------------------------------------------------------------------------


def test_an_event_from_another_interface_never_reaches_the_queue(pub, transport):
    """`pub` is process-global; this adapter process also hosts the Serial and
    BLE transports (their interfaces publish on the same topics)."""
    other = FakeInterface()

    deliver_text(pub, transport, interface=other)
    deliver_waypoint(pub, transport, interface=other)
    pub.send(TEXT_TOPIC, text_packet(), None)
    pub.send(TEXT_TOPIC, text_packet(), object())

    batch = transport.drain_received()

    assert batch.events == () and batch.malformed == 0
    assert transport.get_receive_stats()["received_text"] == 0


def test_only_the_current_interface_is_accepted(pub, transport):
    other = FakeInterface()
    deliver_text(pub, transport, text_packet(id=1), interface=other)
    deliver_text(pub, transport, text_packet(id=2))

    assert [e.packet_id for e in transport.drain_received().events] == [2]


def test_a_late_packet_from_a_released_interface_is_dropped(pub, transport):
    old = transport._interface
    transport._interface = FakeInterface()  # reconnected: a new interface object
    deliver_text(pub, transport, text_packet(id=1), interface=old)
    deliver_text(pub, transport, text_packet(id=2))

    assert [e.packet_id for e in transport.drain_received().events] == [2]

    transport._interface = None  # disconnected
    deliver_text(pub, transport, text_packet(id=3), interface=old)
    assert transport.drain_received().events == ()


def test_the_interface_still_handshaking_is_recognised(pub, transport):
    building = FakeInterface()
    transport._pending_interface = building  # set by the interface constructor before its handshake

    deliver_text(pub, transport, text_packet(id=1), interface=building)

    assert [e.packet_id for e in transport.drain_received().events] == [1]


def test_the_pending_interface_is_registered_by_the_real_constructor_path(pub, fake_connectable):
    transport = TCPTransport(host="192.168.2.34")
    seen_pending = []

    class Probe(FakeInterface):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            seen_pending.append(transport._pending_interface)

    sys.modules["meshtastic.tcp_interface"].TCPInterface = Probe

    transport.connect(_descriptor(), timeout=5)

    assert seen_pending and seen_pending[0] is transport._interface
    assert transport._pending_interface is None, "cleared once the interface is current"


# ---------------------------------------------------------------------------
# Bounded queue, drop-oldest, counters (plan section 69)
# ---------------------------------------------------------------------------


def test_the_queue_is_bounded_drops_the_oldest_and_counts(pub, transport):
    for index in range(300):
        deliver_text(pub, transport, text_packet(id=index + 1))

    stats = transport.get_receive_stats()
    assert stats["queue_depth"] == 256 and stats["queue_overflow_dropped"] == 44

    batch = transport.drain_received(limit=1000)

    assert len(batch.events) == 256
    assert batch.dropped == 44
    assert batch.events[0].packet_id == 45 and batch.events[-1].packet_id == 300, "oldest dropped, freshest kept"

    again = transport.drain_received()
    assert again.events == () and again.dropped == 0, "the counter is per drain, not cumulative"
    assert transport.get_receive_stats()["queue_overflow_dropped"] == 44, "the lifetime counter is"


def test_overflow_warns_once_per_interval_not_once_per_event(pub, transport, logs, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(tcp_mod.time, "monotonic", lambda: clock[0])

    for index in range(256 + 50):
        deliver_text(pub, transport, text_packet(id=index + 1))
    assert len([1 for level, _ in logs if level == "WARNING"]) == 1

    clock[0] += 61.0
    deliver_text(pub, transport, text_packet(id=9999))
    assert len([1 for level, _ in logs if level == "WARNING"]) == 2
    assert all("hello mesh" not in message for _, message in logs), "message text is never logged"


def test_text_and_waypoints_share_one_ordered_queue(pub, transport):
    deliver_text(pub, transport, text_packet(id=1))
    deliver_waypoint(pub, transport)
    deliver_text(pub, transport, text_packet(id=3))

    kinds = [type(e).__name__ for e in transport.drain_received().events]

    assert kinds == ["ReceivedTextEvent", "ReceivedWaypointEvent", "ReceivedTextEvent"]


def test_drain_limit_leaves_the_rest_for_the_next_call(pub, transport):
    for index in range(7):
        deliver_text(pub, transport, text_packet(id=index + 1))

    first = transport.drain_received(limit=3)
    second = transport.drain_received(limit=3)
    third = transport.drain_received(limit=3)

    assert [len(b.events) for b in (first, second, third)] == [3, 3, 1]
    assert [e.packet_id for b in (first, second, third) for e in b.events] == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.parametrize("bad_limit", [0, -1, "5", None, 2.5, True])
def test_a_bad_limit_is_rejected(pub, transport, bad_limit):
    with pytest.raises(TransportError):
        transport.drain_received(limit=bad_limit)


def test_stats_count_everything_without_content(pub, transport):
    deliver_text(pub, transport)
    deliver_waypoint(pub, transport)
    deliver_text(pub, transport, {"decoded": {}})
    transport.drain_received()

    assert transport.get_receive_stats() == {
        "received_text": 1, "received_waypoint": 1, "queue_overflow_dropped": 0,
        "drained_events": 2, "malformed_events": 1, "queue_depth": 0,
    }


def test_draining_never_touches_the_radio_or_needs_a_connection(pub, transport):
    interface = transport._interface
    deliver_text(pub, transport)
    transport._interface = None  # disconnected

    batch = transport.drain_received()

    assert len(batch.events) == 1, "captured events survive a disconnect"
    assert interface.calls == []


# ---------------------------------------------------------------------------
# Reconnect to the same radio keeps the queue (plan section 67)
# ---------------------------------------------------------------------------


def test_events_queued_before_a_reconnect_are_delivered_with_those_after(pub, fake_connectable):
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    first_interface = transport._interface
    first_generation = transport.drain_received().connection_generation

    pub.send(TEXT_TOPIC, text_packet(id=1), first_interface)  # event A
    transport.reconnect(timeout=30)
    second_interface = transport._interface
    pub.send(TEXT_TOPIC, text_packet(id=2), second_interface)  # event B
    pub.send(TEXT_TOPIC, text_packet(id=3), first_interface)  # a late one from the released session

    batch = transport.drain_received()

    assert second_interface is not first_interface
    assert [e.packet_id for e in batch.events] == [1, 2], "A + B; the late packet from the old session is dropped"
    assert batch.connection_generation == first_generation + 1, "a new physical connection bumps the generation"
    assert len(pub.listeners) == 2, "and the subscription was not re-made"


def test_a_failed_reconnect_does_not_lose_queued_events(pub, fake_connectable, monkeypatch):
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    pub.send(TEXT_TOPIC, text_packet(id=1), transport._interface)
    monkeypatch.setattr(tcp_mod.socket, "create_connection", lambda *a, **k: (_ for _ in ()).throw(ConnectionRefusedError()))

    with pytest.raises(TransportError):
        transport.reconnect(timeout=6)

    assert [e.packet_id for e in transport.drain_received().events] == [1]


# ---------------------------------------------------------------------------
# IPC operation, client and router (plan section 70)
# ---------------------------------------------------------------------------


def _dispatcher_for(target):
    dispatcher = _AdapterDispatcher.__new__(_AdapterDispatcher)
    dispatcher._target = lambda transport_type: target
    return dispatcher


def _request(operation, params=None, transport_type="tcp", timeout=10.0):
    return {
        "protocol_version": ipc_protocol.PROTOCOL_VERSION, "operation": operation,
        "transport_type": transport_type, "params": params or {}, "timeout": timeout,
    }


def test_the_ipc_operation_returns_the_batch_as_plain_json(pub, transport):
    deliver_text(pub, transport)
    deliver_waypoint(pub, transport)

    response = _dispatcher_for(transport).handle(_request("drain_received", {"limit": 10}))
    wire = json.loads(json.dumps(response))

    assert wire["ok"] is True
    batch = ipc_protocol.received_batch_from_dict(wire["result"])
    assert [type(e).__name__ for e in batch.events] == ["ReceivedTextEvent", "ReceivedWaypointEvent"]


def test_the_ipc_operation_passes_limit_and_defaults_to_100(pub, transport):
    for index in range(150):
        deliver_text(pub, transport, text_packet(id=index + 1))
    dispatcher = _dispatcher_for(transport)

    default = ipc_protocol.received_batch_from_dict(dispatcher.handle(_request("drain_received"))["result"])
    limited = ipc_protocol.received_batch_from_dict(dispatcher.handle(_request("drain_received", {"limit": 5}))["result"])

    assert (len(default.events), len(limited.events)) == (100, 5)


def test_a_transport_that_cannot_receive_answers_unsupported_over_ipc():
    from meshsrv.radio_transport import RadioTransport

    # Serial/Bluetooth today: every abstract method present, drain_received left at the default.
    cannot_receive = type(
        "CannotReceive", (RadioTransport,),
        {name: (lambda self, *a, **k: None) for name in RadioTransport.__abstractmethods__},
    )()

    response = _dispatcher_for(cannot_receive).handle(_request("drain_received", transport_type="serial"))

    assert response["ok"] is False
    assert response["error"]["code"] == TransportErrorCode.UNSUPPORTED.value


class _Supervisor:
    def __init__(self, result=None, error=None):
        self.requests = []
        self.result = result
        self.error = error

    def call(self, request, *, timeout, ble_address_for_cleanup):
        self.requests.append(request)
        if self.error:
            raise self.error
        return {"ok": True, "result": self.result}


def _event(packet_id=1):
    return ReceivedTextEvent(
        from_node_id="!1fa065f0", to_node_id="^all", text="hi", received_at=1.0,
        local_radio_node_id=LOCAL_ID, packet_id=packet_id,
    )


def test_the_core_proxy_sends_a_plain_request_and_rebuilds_the_batch():
    batch = ReceivedBatch(events=(_event(1), _event(2)), dropped=3, connection_generation=4)
    supervisor = _Supervisor(result=ipc_protocol.received_batch_to_dict(batch))
    client = AdapterIPCTransport(ConnectionType.TCP, supervisor)

    result = client.drain_received(limit=50, timeout=5.0)

    assert result == batch
    request = supervisor.requests[0]
    assert (request["operation"], request["transport_type"], request["params"]) == ("drain_received", "tcp", {"limit": 50})


def test_unsupported_does_not_mark_the_link_as_failed():
    supervisor = _Supervisor(error=TransportError(TransportErrorCode.UNSUPPORTED, "cannot receive"))
    client = AdapterIPCTransport(ConnectionType.SERIAL, supervisor)
    client._cached_info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=None, node_id=None)

    with pytest.raises(TransportError):
        client.drain_received()

    assert client.get_connection_info().state == ConnectionState.CONNECTED


def test_a_real_failure_still_marks_the_link_as_failed():
    supervisor = _Supervisor(error=TransportError(TransportErrorCode.ADAPTER_UNAVAILABLE, "gone"))
    client = AdapterIPCTransport(ConnectionType.TCP, supervisor)
    client._cached_info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=None, node_id=None)

    with pytest.raises(TransportError):
        client.drain_received()

    assert client.get_connection_info().state == ConnectionState.ERROR


def test_the_router_delegates_drain_with_the_same_lock_behaviour_as_everything_else():
    class Active:
        def __init__(self):
            self.calls = []

        def drain_received(self, *, limit=100, timeout=5.0):
            self.calls.append((limit, timeout))
            return ReceivedBatch(events=(_event(),))

    active = Active()
    router = TransportRouter(active)

    assert len(router.drain_received(limit=7, timeout=3.0).events) == 1
    assert active.calls and active.calls[0][0] == 7 and active.calls[0][1] <= 3.0

    router._lock.acquire()  # a switch / long reconnect is in flight
    try:
        started = time.monotonic()
        with pytest.raises(TransportError) as excinfo:
            router.drain_received(timeout=0.3)
        assert excinfo.value.code == TransportErrorCode.BUSY
        assert time.monotonic() - started < 2.0, "bounded, never hangs behind the lock"
    finally:
        router._lock.release()


def test_an_asynchronous_pubsub_event_never_disturbs_the_request_response_ipc(pub, transport):
    """The pubsub callback fires on another thread whenever a packet arrives.
    Nothing may reach stdout except one response line per request, in order."""
    stop = threading.Event()
    injected = []

    def pump():
        index = 0
        while not stop.is_set():
            index += 1
            deliver_text(pub, transport, text_packet(id=index))
            injected.append(index)
            time.sleep(0.0005)

    requests = []
    for step in range(60):
        requests.append(_request("drain_received", {"limit": 20}))
        requests.append(_request("connection_info"))
    stdin = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    stdout = io.StringIO()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    try:
        serve_forever(_dispatcher_for(transport), stdin=stdin, stdout=stdout)
    finally:
        stop.set()
        thread.join(timeout=5)

    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    responses = [json.loads(line) for line in lines]  # every line is JSON: nothing else was written
    assert len(responses) == len(requests)
    assert all(set(r) >= {"ok"} and "kind" not in r and "events" not in r for r in responses)

    drained = []
    for response, request in zip(responses, requests):
        assert response["ok"] is True
        if request["operation"] == "drain_received":
            drained.extend(e.packet_id for e in ipc_protocol.received_batch_from_dict(response["result"]).events)
        else:
            assert "state" in response["result"]  # a connection_info answer, not an event

    leftover = [e.packet_id for e in transport.drain_received(limit=1000).events]
    dropped_total = transport.get_receive_stats()["queue_overflow_dropped"]
    assert drained + leftover == sorted(drained + leftover), "order preserved"
    assert len(drained) + len(leftover) + dropped_total == len(injected), "every captured event is accounted for"
