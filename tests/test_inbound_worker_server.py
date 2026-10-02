"""TCP inbound wired into server.py: eligibility, the whole path from a drained
event to messages / chats / waypoints.db, the safety checks (a radio that is not
the accepted one, an identity refusal), and the existing REST endpoints that
expose the result (plan sections 68, 71-75).
"""
import json

import pytest

from meshsrv import inbound_events, inbound_worker as iw
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    ReceivedBatch,
    ReceivedNodeInfoEvent,
    ReceivedPositionEvent,
    ReceivedTelemetryEvent,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
    TransportError,
    TransportErrorCode,
)

REMOTE = "!1fa065f0"
OTHER = "!2b3c4d5e"


class Queue:
    """Stands in for the adapter's receive queue behind transport_router.drain_received()."""

    def __init__(self):
        self.pending = []
        self.calls = 0
        self.raises = None

    def push(self, *events):
        self.pending.extend(events)

    def drain_received(self, *, limit=100, timeout=5.0):
        self.calls += 1
        if self.raises:
            raise self.raises
        taken, self.pending = self.pending[:limit], self.pending[limit:]
        return ReceivedBatch(events=tuple(taken), connection_generation=1)


@pytest.fixture
def tcp(server_module, monkeypatch):
    """A TCP-accepted radio, identity MATCH, router on TCP, and a fake receive queue."""
    srv = server_module
    original_identity = srv.instance_manager.get()
    original_result = srv.RADIO_IDENTITY_RESULT

    updated = dict(original_identity)
    updated["radio"] = {
        **dict(original_identity.get("radio") or {}),
        "node_id": srv.LOCAL_NODE_ID, "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    }
    srv.INSTANCE_IDENTITY = srv.instance_manager.save(updated)
    srv.RADIO_IDENTITY_RESULT = {"status": "MATCH", "detected": {}, "error": None}

    queue = Queue()
    monkeypatch.setattr(srv.transport_router, "drain_received", queue.drain_received)
    monkeypatch.setattr(
        srv.transport_router, "get_connection_info",
        lambda: ConnectionInfo(
            state=ConnectionState.CONNECTED, node_id=srv.LOCAL_NODE_ID,
            descriptor=ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403"),
        ),
    )
    srv.nodes.clear(); srv.chats.clear(); srv.messages.clear(); srv.seen_ids.clear(); srv.seen_recent_texts.clear()
    srv.waypoint_store.delete_all()
    inbound_events.reset_inbound_stats()
    srv.inbound_worker.__init__(
        drain=srv.inbound_worker._drain, eligibility=srv.inbound_worker._eligibility,
        ingest_text=srv.inbound_worker._ingest_text, ingest_waypoint=srv.inbound_worker._ingest_waypoint,
        ingest_nodeinfo=srv.inbound_worker._ingest_nodeinfo, ingest_position=srv.inbound_worker._ingest_position,
        ingest_telemetry=srv.inbound_worker._ingest_telemetry,
        log=lambda *a, **k: None, log_system_event=lambda **k: None,
    )
    yield srv, queue

    srv.waypoint_store.delete_all()
    srv.instance_manager.save(original_identity)
    srv.INSTANCE_IDENTITY = original_identity
    srv.RADIO_IDENTITY_RESULT = original_result


def _text(srv, text="hello mesh", *, packet_id=101, to="^all", channel=1, sender=REMOTE, radio=None, **kw):
    return ReceivedTextEvent(
        from_node_id=sender, to_node_id=to, text=text, received_at=1790455379.0,
        local_radio_node_id=radio or srv.active_radio_node_id(), packet_id=packet_id,
        from_num=int(sender[1:], 16), channel_index=channel, rx_rssi=-80, rx_snr=5.5, hop_start=3, relay_node=240, **kw,
    )


def _waypoint(srv, waypoint_id=930001, name="Cafe", lat=50.4501, radio=None):
    return ReceivedWaypointEvent(
        waypoint_id=waypoint_id, sender_id=REMOTE, name=name, description="meet here", received_at=1790455379.0,
        local_radio_node_id=radio or srv.active_radio_node_id(), packet_id=102,
        latitude=lat, longitude=30.5234, icon=128205, expire_at=4102444800, channel_index=1,
    )


def _nodeinfo(srv, node_id=REMOTE, *, long_name="Test Node", radio=None, packet_id=201):
    return ReceivedNodeInfoEvent(
        node_id=node_id, sender_id=node_id, received_at=1790455379.0,
        local_radio_node_id=radio or srv.active_radio_node_id(), packet_id=packet_id,
        long_name=long_name, short_name="TST", hw_model="RAK4631", role="ROUTER",
    )


def _position(srv, *, sender=REMOTE, lat=50.4501, lon=30.5234, radio=None, packet_id=202):
    return ReceivedPositionEvent(
        sender_id=sender, received_at=1790455379.0, local_radio_node_id=radio or srv.active_radio_node_id(),
        packet_id=packet_id, latitude=lat, longitude=lon,
    )


def _telemetry(srv, *, sender=REMOTE, kind="device", metrics=None, radio=None, packet_id=203):
    return ReceivedTelemetryEvent(
        sender_id=sender, kind=kind, metrics=metrics or {"batteryLevel": 80, "voltage": 3.9},
        received_at=1790455379.0, local_radio_node_id=radio or srv.active_radio_node_id(), packet_id=packet_id,
    )


def _stored(srv):
    return [{k: v for k, v in m.items() if k not in ("id", "time")} for m in srv.messages]


# --- eligibility -------------------------------------------------------------------


def test_eligible_when_tcp_identity_match_and_the_router_is_on_tcp(tcp):
    srv, _ = tcp

    assert srv.inbound_eligibility() is None


@pytest.mark.parametrize("transport", ["serial", "bluetooth"])
def test_not_eligible_for_other_accepted_transports(tcp, transport):
    srv, _ = tcp
    updated = dict(srv.instance_manager.get())
    updated["radio"] = {**updated["radio"], "transport": transport,
                        "endpoint": {"port": "/dev/ttyACM0"} if transport == "serial" else {"address": "AA:BB"}}
    srv.INSTANCE_IDENTITY = srv.instance_manager.save(updated)

    assert srv.inbound_eligibility() == "not_tcp"


@pytest.mark.parametrize("status", ["MISMATCH", "NOT_FOUND", "DETECTION_ERROR", "NOT_CHECKED"])
def test_not_eligible_unless_identity_is_match(tcp, status):
    srv, queue = tcp
    srv.RADIO_IDENTITY_RESULT = {"status": status}
    queue.push(_text(srv))

    assert srv.inbound_eligibility() == f"identity_{status.lower()}"
    assert srv.inbound_worker.tick() == "idle"
    assert queue.calls == 0 and srv.messages == [], "an unverified radio is never even drained"


def test_not_eligible_while_the_router_is_not_on_tcp(tcp, monkeypatch):
    srv, queue = tcp
    monkeypatch.setattr(
        srv.transport_router, "get_connection_info",
        lambda: ConnectionInfo(
            state=ConnectionState.CONNECTED, node_id=None,
            descriptor=ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyACM0"),
        ),
    )

    assert srv.inbound_eligibility() == "router_not_tcp"
    monkeypatch.setattr(srv.transport_router, "get_connection_info", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert srv.inbound_eligibility() == "router_unavailable"
    assert queue.calls == 0


# --- the whole path: event -> messages / chats / nodes -----------------------------------


def test_a_channel_text_becomes_a_message_with_unread_and_a_node(tcp):
    srv, queue = tcp
    queue.push(_text(srv))

    assert srv.inbound_worker.tick() == "ingesting"

    [message] = _stored(srv)
    assert (message["kind"], message["node_id"], message["text"], message["packet_id"]) == ("rx", REMOTE, "hello mesh", 101)
    assert message["chat_id"] == "channel:1" and message["chat_type"] == "channel"
    assert srv.chats["channel:1"]["unread"] == 1 and srv.chats["channel:1"]["last_message"] == "hello mesh"
    node = srv.nodes[REMOTE]
    assert (node["rssi"], node["snr"], node["hop_start"], node["relay_node"]) == ("-80", "5.5", "3", "240")
    assert 101 in srv.seen_ids


def test_a_direct_message_goes_to_the_senders_chat(tcp):
    srv, queue = tcp
    queue.push(_text(srv, "psst", to=srv.active_radio_node_id(), channel=0, packet_id=5))

    srv.inbound_worker.tick()

    [message] = _stored(srv)
    assert message["chat_id"] == REMOTE and message["chat_type"] == "dm"
    assert srv.chats[REMOTE]["unread"] == 1


def test_unicode_survives_exactly(tcp):
    srv, queue = tcp
    queue.push(_text(srv, "Привет TCP 👋"))

    srv.inbound_worker.tick()

    assert _stored(srv)[0]["text"] == "Привет TCP 👋"


def test_a_reply_is_linked_to_the_original(tcp):
    srv, queue = tcp
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)
    queue.push(_text(srv, "the answer", packet_id=9, reply_id=555, channel=0))

    srv.inbound_worker.tick()

    assert srv.messages[-1]["reply_to"]["packet_id"] == 555


def test_the_same_packet_delivered_twice_is_stored_once_even_across_batches(tcp):
    srv, queue = tcp
    queue.push(_text(srv, packet_id=7))
    srv.inbound_worker.tick()
    queue.push(_text(srv, packet_id=7))  # replayed by the radio, arrives in a later drain
    srv.inbound_worker.tick()

    assert len(srv.messages) == 1 and srv.chats["channel:1"]["unread"] == 1


def test_without_a_packet_id_the_same_text_within_15s_is_stored_once(tcp):
    srv, queue = tcp
    queue.push(_text(srv, "no id", packet_id=None), _text(srv, "no id", packet_id=None))

    srv.inbound_worker.tick()

    assert len(srv.messages) == 1 and len(srv.seen_ids) == 0


def test_an_ignored_node_stores_nothing(tcp):
    srv, queue = tcp
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Muted", "ignored": True}
    queue.push(_text(srv, "spam"))

    srv.inbound_worker.tick()

    assert srv.messages == []


@pytest.fixture
def mca(tcp, monkeypatch):
    srv, queue = tcp
    calls = []
    monkeypatch.setattr(
        srv.mca_runtime, "handle_incoming_meshtastic_text",
        lambda text, node_id, router, **kw: calls.append((text, node_id, router, kw)),
    )
    return srv, queue, calls


def test_an_mca1_direct_message_reaches_mcattach(mca):
    srv, queue, calls = mca
    queue.push(_text(srv, "MCA1:control", to=srv.active_radio_node_id(), channel=1, packet_id=31))

    srv.inbound_worker.tick()

    assert len(calls) == 1
    text, node_id, router, kwargs = calls[0]
    assert (text, node_id) == ("MCA1:control", REMOTE) and router is srv.transport_router
    assert kwargs["packet_id"] == 31 and kwargs["channel_index"] == 1
    assert srv.messages[-1]["text"] == "MCA1:control"


def test_an_mca1_text_on_a_channel_is_only_a_message(mca):
    srv, queue, calls = mca
    queue.push(_text(srv, "MCA1:control", packet_id=32))

    srv.inbound_worker.tick()

    assert calls == [] and len(srv.messages) == 1


# --- waypoints --------------------------------------------------------------------------


def test_a_waypoint_is_stored_and_shown_by_the_existing_endpoint(tcp):
    srv, queue = tcp
    queue.push(_waypoint(srv))

    srv.inbound_worker.tick()

    row = srv.waypoint_store.get(930001)
    assert (row["sender_id"], row["name"], row["channel_index"], row["icon"]) == (REMOTE, "Cafe", 1, 128205)
    assert row["latitude"] == pytest.approx(50.4501)
    assert json.loads(row["raw_packet"]) == {"source": "tcp", "packet_id": 102, "sender_id": REMOTE, "channel_index": 1}


def test_a_repeated_waypoint_is_a_duplicate_and_a_changed_one_updates_the_row(tcp):
    srv, queue = tcp
    queue.push(_waypoint(srv), _waypoint(srv))
    srv.inbound_worker.tick()
    assert srv.waypoint_store.count() == 1

    queue.push(_waypoint(srv, name="Bar", lat=50.5))
    srv.inbound_worker.tick()

    assert srv.waypoint_store.count() == 1
    row = srv.waypoint_store.get(930001)
    assert row["name"] == "Bar" and row["latitude"] == pytest.approx(50.5)
    stats = inbound_events.get_inbound_stats()
    assert (stats["waypoint_created"], stats["waypoint_duplicate"], stats["waypoint_updated"]) == (1, 1, 1)


def test_a_waypoint_without_a_position_is_skipped(tcp):
    srv, queue = tcp
    event = _waypoint(srv)
    queue.push(ReceivedWaypointEvent(**{**event.__dict__, "latitude": None, "longitude": None, "expire_at": 0}))

    srv.inbound_worker.tick()

    assert srv.waypoint_store.count() == 0


# --- nodeinfo / position / telemetry (PR D) ------------------------------------------------


def test_a_nodeinfo_creates_a_node(tcp):
    srv, queue = tcp
    queue.push(_nodeinfo(srv))

    assert srv.inbound_worker.tick() == "ingesting"

    node = srv.nodes[REMOTE]
    assert (node["name"], node["short_name"], node["hw_model"], node["role"]) == ("Test Node", "TST", "RAK4631", "ROUTER")
    assert srv.inbound_worker.stats()["nodeinfo_events"] == 1
    assert inbound_events.get_inbound_stats()["nodeinfo_stored"] == 1


def test_a_position_sets_the_nodes_position(tcp):
    srv, queue = tcp
    queue.push(_position(srv))

    srv.inbound_worker.tick()

    assert srv.nodes[REMOTE]["position"] == {"latitude": pytest.approx(50.4501), "longitude": pytest.approx(30.5234)}
    assert srv.inbound_worker.stats()["position_events"] == 1


def test_telemetry_updates_the_nodes_metrics(tcp, monkeypatch):
    srv, queue = tcp
    monkeypatch.setattr(srv.telemetry, "add_node_telemetry_record", lambda *a, **k: None)
    queue.push(_telemetry(srv))

    srv.inbound_worker.tick()

    node = srv.nodes[REMOTE]
    assert node["battery_level"] == 80 and node["voltage"] == 3.9 and node["telemetry_source"] == "tcp"
    assert srv.inbound_worker.stats()["telemetry_events"] == 1


def test_the_local_node_is_skipped_for_nodeinfo_but_not_for_telemetry(tcp, monkeypatch):
    srv, queue = tcp
    monkeypatch.setattr(srv.telemetry, "add_node_telemetry_record", lambda *a, **k: None)
    local = srv.active_radio_node_id()
    queue.push(_nodeinfo(srv, node_id=local, packet_id=1), _telemetry(srv, sender=local, packet_id=2))

    srv.inbound_worker.tick()

    assert inbound_events.get_inbound_stats()["nodeinfo_skipped_local"] == 1
    assert local not in srv.nodes or srv.nodes[local].get("short_name") != "TST", "nodeinfo never wrote the local node"
    assert srv.nodes[local]["battery_level"] == 80, "apply_node_telemetry() still updates the local node's own metrics"


# --- safety: stale radio (plan 43 / 68) ---------------------------------------------------


def test_events_from_another_radio_are_dropped_before_anything_is_stored(tcp):
    srv, queue = tcp
    queue.push(_text(srv, "for another radio", radio="!0badf00d"), _waypoint(srv, radio="!0badf00d"),
               _nodeinfo(srv, radio="!0badf00d", packet_id=99), _position(srv, radio="!0badf00d", packet_id=98),
               _telemetry(srv, radio="!0badf00d", packet_id=97),
               _text(srv, "for this radio", packet_id=2))

    srv.inbound_worker.tick()

    assert [m["text"] for m in _stored(srv)] == ["for this radio"]
    assert srv.waypoint_store.count() == 0
    # The legit text still updates REMOTE's node; the 3 new stale kinds never
    # touched it - none of their own fields made it in (short_name/position
    # are also set by the text path itself, to their own text-path defaults).
    node = srv.nodes[REMOTE]
    assert node["short_name"] != "TST" and node.get("hw_model") != "RAK4631" and node.get("role") != "ROUTER"
    assert node.get("position") is None
    assert "battery_level" not in node
    assert inbound_events.get_inbound_stats()["stale_identity_dropped"] == 5
    assert srv.inbound_worker.stats()["drained_events"] == 6


def test_events_queued_for_radio_a_are_dropped_after_switching_to_radio_b(tcp, monkeypatch):
    srv, queue = tcp
    queue.push(_text(srv, "queued for A", packet_id=1), _waypoint(srv))
    identity = dict(srv.INSTANCE_IDENTITY)
    identity["radio"] = {**identity["radio"], "node_id": "!b0b0b0b0"}
    monkeypatch.setattr(srv, "INSTANCE_IDENTITY", identity)
    monkeypatch.setattr(srv, "LOCAL_NODE_ID", "!b0b0b0b0")
    updated = dict(srv.instance_manager.get())
    updated["radio"] = {**updated["radio"], "node_id": "!b0b0b0b0"}
    srv.instance_manager.save(updated)

    srv.inbound_worker.tick()

    assert srv.messages == [] and srv.waypoint_store.count() == 0
    assert inbound_events.get_inbound_stats()["stale_identity_dropped"] == 2


# --- safety: identity refusal discards the queue (plan 46) --------------------------------------


class TcpAdapterProxy:
    """tcp_ipc_transport with a receive queue: what a torn-down session captured."""

    def __init__(self, events):
        self.pending = list(events)
        self.disconnects = 0
        self.drains = 0

    def disconnect(self, *, timeout=15.0):
        self.disconnects += 1

    def drain_received(self, *, limit=100, timeout=5.0):
        self.drains += 1
        taken, self.pending = self.pending[:limit], self.pending[limit:]
        return ReceivedBatch(events=tuple(taken))


def test_an_identity_refusal_closes_the_session_and_discards_what_it_captured(tcp, monkeypatch):
    srv, _ = tcp
    wrong_radio = [_text(srv, f"from the wrong radio {i}", packet_id=i, radio="!0badf00d") for i in range(1, 8)]
    proxy = TcpAdapterProxy(wrong_radio)
    monkeypatch.setattr(srv, "tcp_ipc_transport", proxy)
    events = []
    monkeypatch.setattr(srv, "log_system_event", lambda title, level="INFO", details="", source="system": events.append(details))

    srv.teardown_unverified_tcp_session("MISMATCH", "reconnect")

    assert proxy.disconnects == 1
    assert proxy.pending == [], "the wrong radio's captured events do not survive the teardown"
    assert srv.inbound_worker.stats()["discarded_on_identity_refusal"] == 7
    assert any("7 buffered inbound event(s) discarded" in d for d in events)
    assert srv.messages == [], "and they were discarded, not ingested"


def test_a_refused_identity_after_a_boot_check_also_discards(tcp, monkeypatch):
    srv, _ = tcp
    proxy = TcpAdapterProxy([_text(srv, radio="!0badf00d")])
    monkeypatch.setattr(srv, "tcp_ipc_transport", proxy)
    monkeypatch.setattr(srv, "TCP_IDENTITY_BOOT_RETRY_DELAYS_S", ())
    monkeypatch.setattr(srv, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(
        srv, "detect_tcp_radio_identity",
        lambda transport, host, port, timeout=25: ({
            "status": "MATCH", "checked_at": "2026-09-27T00:00:00+00:00", "configured": {},
            "detected": {"node_id": "!0badf00d", "long_name": "Other"}, "error": None, "error_code": None,
        }, ""),
    )

    srv.verify_radio_identity()

    assert srv.RADIO_IDENTITY_RESULT["status"] == "MISMATCH"
    assert proxy.pending == [] and proxy.disconnects == 1


def test_matching_identity_never_discards(tcp, monkeypatch):
    srv, _ = tcp
    proxy = TcpAdapterProxy([_text(srv)])
    monkeypatch.setattr(srv, "tcp_ipc_transport", proxy)
    monkeypatch.setattr(srv, "log_system_event", lambda *a, **k: None)

    assert srv.refresh_identity_after_reconnect("tcp", {"node_id": srv.LOCAL_NODE_ID}) == "MATCH"

    assert len(proxy.pending) == 1 and proxy.drains == 0


# --- waiting quietly against the real router --------------------------------------------------------


def test_a_busy_router_makes_the_worker_wait_not_fail(tcp, monkeypatch):
    srv, queue = tcp
    monkeypatch.undo()  # use the REAL router.drain_received (the fixture patched it)
    monkeypatch.setattr(iw, "DRAIN_TIMEOUT_S", 0.2)
    monkeypatch.setattr(
        srv.transport_router, "get_connection_info",
        lambda: ConnectionInfo(
            state=ConnectionState.CONNECTED, node_id=None,
            descriptor=ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403"),
        ),
    )
    srv.transport_router._lock.acquire()  # a switch / long reconnect is in flight
    try:
        assert srv.inbound_worker.tick() == "waiting"
    finally:
        srv.transport_router._lock.release()

    assert srv.inbound_worker.stats()["waiting_reason"] == "busy"


def test_a_disconnected_link_makes_it_wait_and_recover(tcp):
    srv, queue = tcp
    queue.raises = TransportError(TransportErrorCode.NOT_CONNECTED, "TCPTransport is not connected")

    assert srv.inbound_worker.tick() == "waiting"

    queue.raises = None
    queue.push(_text(srv))
    assert srv.inbound_worker.tick() == "ingesting" and len(srv.messages) == 1


# --- existing REST endpoints show the result (plan 75) -------------------------------------------------


def _json(srv, endpoint, path):
    with srv.app.test_request_context(path):
        response = srv.app.view_functions[endpoint]()
    return response.get_json() if hasattr(response, "get_json") else response[0].get_json()


def test_the_existing_endpoints_expose_tcp_messages_chats_and_waypoints(tcp, monkeypatch):
    srv, queue = tcp
    # /api/chats kicks off channel discovery through the router; keep this test off the (slow, serial) radio.
    monkeypatch.setattr(srv.transport_router, "get_channels", lambda **kwargs: [])
    queue.push(_text(srv, "visible to the UI", packet_id=41, channel=0), _waypoint(srv, 930099))

    srv.inbound_worker.tick()

    messages = _json(srv, "api_messages", "/api/messages")
    payload = messages["messages"] if isinstance(messages, dict) and "messages" in messages else messages
    assert any(m.get("text") == "visible to the UI" and m.get("kind") == "rx" for m in payload)

    chats = _json(srv, "api_chats", "/api/chats")
    chat_list = chats["chats"] if isinstance(chats, dict) and "chats" in chats else chats
    channel = next(c for c in chat_list if c["id"] == srv.CHANNEL_CHAT_ID)
    assert channel["unread"] == 1

    waypoints = _json(srv, "api_waypoints", "/api/waypoints?include_expired=1")
    listed = waypoints["waypoints"] if isinstance(waypoints, dict) and "waypoints" in waypoints else waypoints
    assert any(w["waypoint_id"] == 930099 and w["name"] == "Cafe" for w in listed)


def test_radio_health_reports_inbound_counters_without_any_message_text(tcp):
    srv, queue = tcp
    queue.push(_text(srv, "an entirely private sentence"))
    srv.inbound_worker.tick()

    with srv.app.test_request_context("/api/radio_health"):
        data = srv.api_radio_health().get_json()

    inbound = data["inbound"]
    assert inbound["worker"]["text_events"] == 1 and inbound["worker"]["drained_events"] == 1
    assert inbound["ingest"]["text_stored"] == 1
    assert "private" not in json.dumps(inbound)


def test_radio_health_also_reports_nodeinfo_position_and_telemetry_counters(tcp, monkeypatch):
    srv, queue = tcp
    monkeypatch.setattr(srv.telemetry, "add_node_telemetry_record", lambda *a, **k: None)
    queue.push(_nodeinfo(srv), _position(srv), _telemetry(srv))
    srv.inbound_worker.tick()

    with srv.app.test_request_context("/api/radio_health"):
        data = srv.api_radio_health().get_json()

    worker, ingest = data["inbound"]["worker"], data["inbound"]["ingest"]
    assert (worker["nodeinfo_events"], worker["position_events"], worker["telemetry_events"]) == (1, 1, 1)
    assert (ingest["nodeinfo_stored"], ingest["position_stored"], ingest["telemetry_stored"]) == (1, 1, 1)
