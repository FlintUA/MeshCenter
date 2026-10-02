"""Serial and TCP inbound must leave the SAME state behind (plan sections 71-74).

The same logical message is delivered twice, each time into a clean state:
  A. as a `--listen` CLI line through server._handle_listener_line() (the serial
     parser -> neutral event -> shared ingest);
  B. as the meshtastic library's packet dict through the TCP adapter's REAL
     normalizer -> neutral event -> the same shared ingest.
The resulting messages / chats / nodes / seen ids / waypoints.db rows must match.
Only fields that are inherently transport-specific may differ, and each of those
is asserted explicitly below (waypoint `raw_packet`, MCA channel_index when the
line names none).

Also the safety gate (plan section 43): an event from any radio other than the
active accepted one is dropped before it touches anything.
"""
import json
import types

import pytest

from adapters.meshtastic.tcp_transport import TCPTransport
from meshsrv import inbound_events
from meshsrv.radio_transport import ReceivedNodeInfoEvent, ReceivedTelemetryEvent, ReceivedTextEvent, ReceivedWaypointEvent
from test_serial_inbound_characterization import _text_line, _waypoint_line

REMOTE = "!1fa065f0"
REMOTE_NUM = 0x1FA065F0
OTHER = "!2b3c4d5e"
OTHER_NUM = 0x2B3C4D5E
BROADCAST = 4294967295


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def srv(server_module):
    def reset():
        server_module.nodes.clear()
        server_module.chats.clear()
        server_module.messages.clear()
        server_module.seen_ids.clear()
        server_module.seen_recent_texts.clear()
        server_module.waypoint_store.delete_all()
        inbound_events.reset_inbound_stats()

    server_module.reset_inbound = reset
    reset()
    yield server_module
    reset()


def _local_num(srv):
    return int(srv.active_radio_node_id()[1:], 16)


def _interface(srv, my_node_num=None):
    return types.SimpleNamespace(
        myInfo=types.SimpleNamespace(my_node_num=my_node_num if my_node_num is not None else _local_num(srv))
    )


def _tcp_text_packet(text, *, from_num=REMOTE_NUM, to_num=BROADCAST, packet_id=101, channel=None,
                     rssi=-80, snr=5.5, hop_start=3, relay=240, reply_id=None):
    """What the library delivers on meshtastic.receive.text for the message
    `_text_line()` describes (omitting the keys protobuf omits)."""
    decoded = {"portnum": "TEXT_MESSAGE_APP", "payload": text.encode(), "text": text}
    if reply_id:
        decoded["replyId"] = reply_id
    packet = {
        "from": from_num, "to": to_num, "decoded": decoded, "rxTime": 1790455378,
        "rxSnr": snr, "hopLimit": 3, "rxRssi": rssi, "hopStart": hop_start, "relayNode": relay, "raw": object(),
    }
    if packet_id is not None:
        packet["id"] = packet_id
    if channel:
        packet["channel"] = channel  # channel 0 is the protobuf default, hence omitted
    return packet


def _tcp_event(srv, packet):
    event = TCPTransport(host="192.168.2.34")._normalize_text(packet, _interface(srv))
    assert isinstance(event, ReceivedTextEvent)
    return event


def _tcp_waypoint_event(srv, waypoint_id, **fields):
    waypoint = {
        "id": waypoint_id, "latitudeI": 504501000, "longitudeI": 305234000, "expire": 4102444800,
        "name": "Cafe", "description": "meet here", "icon": 128205, "raw": "id: 1",
    }
    waypoint.update(fields)
    packet = {"from": REMOTE_NUM, "to": BROADCAST, "id": 102, "channel": 1,
              "decoded": {"portnum": "WAYPOINT_APP", "payload": b"y", "waypoint": waypoint}, "raw": object()}
    event = TCPTransport(host="192.168.2.34")._normalize_waypoint(packet, _interface(srv))
    assert isinstance(event, ReceivedWaypointEvent)
    return event


def _snapshot(srv):
    """Everything an inbound text leaves behind, minus timestamps and uuids."""
    messages = [{k: v for k, v in m.items() if k not in ("id", "time")} for m in srv.messages]
    for message in messages:
        if isinstance(message.get("reply_to"), dict):
            message["reply_to"] = {k: v for k, v in message["reply_to"].items() if k not in ("id", "time")}
    chats = {cid: {k: v for k, v in c.items() if k != "last_time"} for cid, c in srv.chats.items()}
    nodes = {nid: {k: v for k, v in n.items() if k not in ("last_seen", "last_time")} for nid, n in srv.nodes.items()}
    return {
        "messages": messages, "chats": chats, "nodes": nodes,
        "seen_ids": sorted(srv.seen_ids), "seen_recent_texts": sorted(srv.seen_recent_texts),
    }


def _via_serial(srv, lines, prepare=None):
    srv.reset_inbound()
    if prepare:
        prepare(srv)
    for line in lines:
        srv._handle_listener_line(line)
    return _snapshot(srv)


def _via_tcp(srv, packets, prepare=None):
    srv.reset_inbound()
    if prepare:
        prepare(srv)
    outcomes = []
    for packet in packets:
        outcomes.append(inbound_events.ingest_received_text(_tcp_event(srv, packet), srv._inbound_deps()))
    return _snapshot(srv), outcomes


def _assert_parity(srv, lines, packets, prepare=None):
    serial = _via_serial(srv, lines, prepare)
    tcp, outcomes = _via_tcp(srv, packets, prepare)
    assert tcp == serial
    return serial, outcomes


# ---------------------------------------------------------------------------
# Text parity (sections 71-72)
# ---------------------------------------------------------------------------


def test_primary_channel_text(srv):
    snapshot, _ = _assert_parity(
        srv, [_text_line("hello mesh")], [_tcp_text_packet("hello mesh")],
    )
    [message] = snapshot["messages"]
    assert (message["kind"], message["node_id"], message["text"], message["packet_id"]) == ("rx", REMOTE, "hello mesh", 101)
    assert message["chat_id"] == srv.CHANNEL_CHAT_ID
    assert snapshot["chats"][srv.CHANNEL_CHAT_ID]["unread"] == 1
    assert snapshot["nodes"][REMOTE]["rssi"] == "-80" and snapshot["nodes"][REMOTE]["snr"] == "5.5"


@pytest.mark.parametrize("channel", [1, 2, 5, 7])
def test_secondary_channel_text(srv, channel):
    snapshot, _ = _assert_parity(
        srv, [_text_line("on a channel", channel=channel)], [_tcp_text_packet("on a channel", channel=channel)],
    )
    assert snapshot["messages"][0]["chat_id"] == f"channel:{channel}"
    assert snapshot["chats"][f"channel:{channel}"]["name"] == f"Channel {channel}"


def test_direct_message(srv):
    local = srv.active_radio_node_id()
    snapshot, _ = _assert_parity(
        srv,
        [_text_line("psst", to_num=_local_num(srv), to_id=local)],
        [_tcp_text_packet("psst", to_num=_local_num(srv))],
    )
    message = snapshot["messages"][0]
    assert message["chat_id"] == REMOTE and message["chat_type"] == "dm"
    assert snapshot["chats"][REMOTE]["unread"] == 1


def test_unicode_text(srv):
    snapshot, _ = _assert_parity(
        srv, [_text_line("Привет TCP 👋")], [_tcp_text_packet("Привет TCP 👋")],
    )
    assert snapshot["messages"][0]["text"] == "Привет TCP 👋"


@pytest.mark.parametrize("snr", [5.5, -7.0, 0.25, 12.75])
def test_signal_values_are_recorded_identically(srv, snr):
    snapshot, _ = _assert_parity(
        srv, [_text_line(snr=snr, rssi=-101, hop_start=7, relay=17)],
        [_tcp_text_packet("hello mesh", snr=snr, rssi=-101, hop_start=7, relay=17)],
    )
    node = snapshot["nodes"][REMOTE]
    assert (node["snr"], node["rssi"], node["hop_start"], node["relay_node"]) == (str(snr), "-101", "7", "17")


def test_a_reply_references_the_original_message(srv):
    def prepare(s):
        s.add_message("me", "Me", "the question", s.LOCAL_NODE_ID, s.CHANNEL_CHAT_ID, packet_id=555)

    snapshot, _ = _assert_parity(
        srv, [_text_line("the answer", packet_id=8, reply_id=555)],
        [_tcp_text_packet("the answer", packet_id=8, reply_id=555)], prepare,
    )
    assert snapshot["messages"][-1]["reply_to"]["packet_id"] == 555
    assert snapshot["messages"][-1]["reply_to"]["text"] == "the question"


def test_same_packet_id_twice_is_one_message_and_the_second_does_not_touch_the_node(srv):
    snapshot, outcomes = _assert_parity(
        srv,
        [_text_line("first", packet_id=7, rssi=-80), _text_line("first", packet_id=7, rssi=-42)],
        [_tcp_text_packet("first", packet_id=7, rssi=-80), _tcp_text_packet("first", packet_id=7, rssi=-42)],
    )
    assert outcomes == [inbound_events.STORED, inbound_events.DUPLICATE_PACKET]
    assert len(snapshot["messages"]) == 1 and snapshot["nodes"][REMOTE]["rssi"] == "-80"


def test_same_text_within_15s_is_one_message_but_still_refreshes_the_node(srv):
    snapshot, outcomes = _assert_parity(
        srv,
        [_text_line("same words", packet_id=1, rssi=-80), _text_line("same words", packet_id=2, rssi=-42)],
        [_tcp_text_packet("same words", packet_id=1, rssi=-80), _tcp_text_packet("same words", packet_id=2, rssi=-42)],
    )
    assert outcomes == [inbound_events.STORED, inbound_events.DUPLICATE_TEXT]
    assert len(snapshot["messages"]) == 1 and snapshot["nodes"][REMOTE]["rssi"] == "-42"


def test_without_a_packet_id_the_text_fallback_dedups(srv):
    snapshot, outcomes = _assert_parity(
        srv,
        [_text_line("no id", packet_id=None), _text_line("no id", packet_id=None)],
        [_tcp_text_packet("no id", packet_id=None), _tcp_text_packet("no id", packet_id=None)],
    )
    assert outcomes == [inbound_events.STORED, inbound_events.DUPLICATE_TEXT]
    assert len(snapshot["messages"]) == 1 and snapshot["seen_ids"] == []


def test_two_nodes_saying_the_same_thing_are_both_kept(srv):
    snapshot, _ = _assert_parity(
        srv,
        [_text_line("hi all", packet_id=1), _text_line("hi all", packet_id=2, from_num=OTHER_NUM, from_id=OTHER)],
        [_tcp_text_packet("hi all", packet_id=1), _tcp_text_packet("hi all", packet_id=2, from_num=OTHER_NUM)],
    )
    assert [m["node_id"] for m in snapshot["messages"]] == [REMOTE, OTHER]


def test_an_ignored_node_updates_but_stores_nothing(srv):
    def prepare(s):
        s.nodes[REMOTE] = {"node_id": REMOTE, "name": "Muted", "ignored": True}

    snapshot, outcomes = _assert_parity(
        srv, [_text_line("spam", rssi=-11)], [_tcp_text_packet("spam", rssi=-11)], prepare,
    )
    assert outcomes == [inbound_events.IGNORED_NODE]
    assert snapshot["messages"] == [] and snapshot["nodes"][REMOTE]["rssi"] == "-11"


def test_a_message_from_our_own_node_does_not_write_the_node(srv):
    local = srv.active_radio_node_id()
    snapshot, _ = _assert_parity(
        srv,
        [_text_line("echo", from_num=_local_num(srv), from_id=local)],
        [_tcp_text_packet("echo", from_num=_local_num(srv))],
    )
    assert local not in snapshot["nodes"]
    assert snapshot["messages"][0]["chat_id"] == srv.CHANNEL_CHAT_ID


def test_a_run_of_mixed_traffic_ends_in_the_same_state(srv):
    lines = [
        _text_line("one", packet_id=1), _text_line("two", packet_id=2, channel=2),
        _text_line("three", packet_id=3, to_num=_local_num(srv), to_id=srv.active_radio_node_id()),
        _text_line("one", packet_id=4), _text_line("four", packet_id=1),
        _text_line("five", packet_id=5, from_num=OTHER_NUM, from_id=OTHER),
    ]
    packets = [
        _tcp_text_packet("one", packet_id=1), _tcp_text_packet("two", packet_id=2, channel=2),
        _tcp_text_packet("three", packet_id=3, to_num=_local_num(srv)),
        _tcp_text_packet("one", packet_id=4), _tcp_text_packet("four", packet_id=1),
        _tcp_text_packet("five", packet_id=5, from_num=OTHER_NUM),
    ]
    snapshot, outcomes = _assert_parity(srv, lines, packets)

    assert outcomes == [
        inbound_events.STORED, inbound_events.STORED, inbound_events.STORED,
        inbound_events.DUPLICATE_TEXT, inbound_events.DUPLICATE_PACKET, inbound_events.STORED,
    ]
    assert [m["text"] for m in snapshot["messages"]] == ["one", "two", "three", "five"]


# --- MCAttach -----------------------------------------------------------------


@pytest.fixture
def mca_calls(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        srv.mca_runtime, "handle_incoming_meshtastic_text",
        lambda text, node_id, router, **kwargs: calls.append((text, node_id, router, kwargs)),
    )
    return calls


def test_a_direct_mca1_text_reaches_mcattach_the_same_way(srv, mca_calls):
    local = srv.active_radio_node_id()
    _via_serial(srv, [_text_line("MCA1:abc", to_num=_local_num(srv), to_id=local, packet_id=31, channel=1)])
    serial_calls, mca_calls[:] = list(mca_calls), []
    _via_tcp(srv, [_tcp_text_packet("MCA1:abc", to_num=_local_num(srv), packet_id=31, channel=1)])

    assert len(serial_calls) == len(mca_calls) == 1
    assert mca_calls == serial_calls, "same text, sender, router and packet_id/channel_index/data_dir"


def test_mca_channel_index_when_the_source_names_no_channel(srv, mca_calls):
    """The one legitimate difference: a CLI line that lacks a channel field says
    "unknown" (None); the library omits channel 0 because it is the protobuf
    default, so the TCP adapter knows it is 0."""
    local = srv.active_radio_node_id()
    _via_serial(srv, [_text_line("MCA1:x", to_num=_local_num(srv), to_id=local, packet_id=32, channel=None)])
    serial_channel = mca_calls[0][3]["channel_index"]
    mca_calls.clear()
    _via_tcp(srv, [_tcp_text_packet("MCA1:x", to_num=_local_num(srv), packet_id=32, channel=None)])

    assert serial_channel is None
    assert mca_calls[0][3]["channel_index"] == 0


def test_a_broadcast_mca1_text_is_not_dispatched_on_either_path(srv, mca_calls):
    _via_serial(srv, [_text_line("MCA1:abc", packet_id=33)])
    _via_tcp(srv, [_tcp_text_packet("MCA1:abc", packet_id=33)])

    assert mca_calls == []


def test_an_mca_failure_never_loses_the_message(srv, monkeypatch):
    monkeypatch.setattr(srv.mca_runtime, "handle_incoming_meshtastic_text",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mca down")))
    snapshot, outcomes = _assert_parity(
        srv,
        [_text_line("MCA1:abc", to_num=_local_num(srv), to_id=srv.active_radio_node_id(), packet_id=34)],
        [_tcp_text_packet("MCA1:abc", to_num=_local_num(srv), packet_id=34)],
    )
    assert outcomes == [inbound_events.STORED] and len(snapshot["messages"]) == 1


# --- radio_event -----------------------------------------------------------------


def test_the_text_signal_fires_for_both_paths(srv, monkeypatch):
    seen = []
    real = srv.radio_event
    monkeypatch.setattr(srv, "radio_event", lambda name, *a, **k: (seen.append(name), real(name, *a, **k))[1])

    _via_serial(srv, [_text_line("ping", packet_id=77)])
    # H1-C2: cli_activity now fires for every CLI line (the old
    # unconditional "packet" signal, renamed); "packet" itself now only
    # fires for a recognized received-packet line like this one.
    assert seen == ["cli_activity", "packet", "text"]
    seen.clear()
    _via_tcp(srv, [_tcp_text_packet("ping", packet_id=77)])
    assert seen == ["text"], "TCP has no CLI line, hence no 'packet'/'cli_activity' signal of its own"


# ---------------------------------------------------------------------------
# What only the TCP side differs in (privacy)
# ---------------------------------------------------------------------------


def test_a_tcp_text_is_never_written_to_the_node_log_but_a_serial_one_still_is(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(srv, "log_node_event", lambda *a, **k: calls.append((a, k)))

    _via_tcp(srv, [_tcp_text_packet("a private message")])
    assert calls and all("a private message" not in json.dumps(c, default=str) for c in calls)
    assert all(k.get("raw") is None for _, k in calls)

    calls.clear()
    _via_serial(srv, [_text_line("a private message")])
    assert any("a private message" in json.dumps(c, default=str) for c in calls), "serial logging is unchanged"


# ---------------------------------------------------------------------------
# Waypoint parity (sections 73-74)
# ---------------------------------------------------------------------------

ROW_COLUMNS = ("waypoint_id", "sender_id", "name", "description", "latitude", "longitude", "icon",
               "expire_at", "channel_index", "is_active")


def _rows(srv):
    return {row["waypoint_id"]: {c: row.get(c) for c in ROW_COLUMNS} for row in srv.waypoint_store.list(include_expired=True)}


def _waypoint_via_serial(srv, lines):
    srv.reset_inbound()
    for line in lines:
        srv._handle_listener_line(line)
    return _rows(srv), {r["waypoint_id"]: r["raw_packet"] for r in srv.waypoint_store.list(include_expired=True)}


def _waypoint_via_tcp(srv, events):
    srv.reset_inbound()
    outcomes = [inbound_events.ingest_received_waypoint(e, srv._inbound_deps()) for e in events]
    return _rows(srv), {r["waypoint_id"]: r["raw_packet"] for r in srv.waypoint_store.list(include_expired=True)}, outcomes


def test_a_waypoint_produces_the_same_row(srv):
    serial_rows, serial_raw = _waypoint_via_serial(srv, [_waypoint_line(waypoint_id=920001)])
    tcp_rows, tcp_raw, outcomes = _waypoint_via_tcp(srv, [_tcp_waypoint_event(srv, 920001)])

    assert tcp_rows == serial_rows
    assert outcomes == [inbound_events.CREATED]
    row = tcp_rows[920001]
    assert row["latitude"] == pytest.approx(50.4501) and row["channel_index"] == 1 and row["sender_id"] == REMOTE
    # the ONE inherent difference: serial keeps the CLI line, TCP a safe provenance snapshot
    assert serial_raw[920001].startswith("Received: {")
    assert json.loads(tcp_raw[920001]) == {"source": "tcp", "packet_id": 102, "sender_id": REMOTE, "channel_index": 1}


def test_a_tcp_waypoint_never_stores_the_library_packet(srv):
    _, tcp_raw, _ = _waypoint_via_tcp(srv, [_tcp_waypoint_event(srv, 920002)])

    stored = tcp_raw[920002]
    for forbidden in ("raw", "payload", "DESCRIPTOR", "bytes", "id: 1"):
        assert forbidden not in stored


def test_repeating_a_waypoint_is_a_duplicate_not_a_second_row(srv):
    serial_rows, _ = _waypoint_via_serial(srv, [_waypoint_line(waypoint_id=920003)] * 2)
    tcp_rows, _, outcomes = _waypoint_via_tcp(srv, [_tcp_waypoint_event(srv, 920003)] * 2)

    assert tcp_rows == serial_rows and len(tcp_rows) == 1
    assert outcomes == [inbound_events.CREATED, inbound_events.DUPLICATE]


def test_a_changed_waypoint_with_the_same_id_updates_the_row(srv):
    serial_rows, _ = _waypoint_via_serial(srv, [
        _waypoint_line(waypoint_id=920004, name="Cafe"),
        _waypoint_line(waypoint_id=920004, name="Bar", lat_i=505000000),
    ])
    tcp_rows, _, outcomes = _waypoint_via_tcp(srv, [
        _tcp_waypoint_event(srv, 920004, name="Cafe"),
        _tcp_waypoint_event(srv, 920004, name="Bar", latitudeI=505000000),
    ])

    assert tcp_rows == serial_rows and len(tcp_rows) == 1
    assert tcp_rows[920004]["name"] == "Bar" and tcp_rows[920004]["latitude"] == pytest.approx(50.5)
    assert outcomes == [inbound_events.CREATED, inbound_events.UPDATED]


def test_waypoints_and_texts_share_a_run_without_interfering(srv):
    serial_state = _via_serial(srv, [_text_line("hello"), _waypoint_line(waypoint_id=920005)])
    serial_rows = _rows(srv)
    srv.reset_inbound()
    inbound_events.ingest_received_text(_tcp_event(srv, _tcp_text_packet("hello")), srv._inbound_deps())
    inbound_events.ingest_received_waypoint(_tcp_waypoint_event(srv, 920005), srv._inbound_deps())

    assert _snapshot(srv) == serial_state and _rows(srv) == serial_rows


def test_a_waypoint_without_a_position_is_skipped_like_the_serial_parser_skips_it(srv):
    line = ("Received: {'from': %d, 'decoded': {'portnum': 'WAYPOINT_APP', 'waypoint': {'id': 920006, 'expire': 0}}, "
            "'id': 5, 'fromId': '%s'}" % (REMOTE_NUM, REMOTE))
    srv.reset_inbound()
    srv._handle_listener_line(line)
    assert srv.waypoint_store.count() == 0

    event = _tcp_waypoint_event(srv, 920006)
    event = ReceivedWaypointEvent(**{**event.__dict__, "latitude": None, "longitude": None, "expire_at": 0})
    assert inbound_events.ingest_received_waypoint(event, srv._inbound_deps()) == inbound_events.NO_POSITION
    assert srv.waypoint_store.count() == 0


# ---------------------------------------------------------------------------
# Safety gate: an event from another radio is never persisted (section 43)
# ---------------------------------------------------------------------------


def _foreign_text(srv, radio="!0badf00d"):
    event = _tcp_event(srv, _tcp_text_packet("from another radio"))
    return ReceivedTextEvent(**{**event.__dict__, "local_radio_node_id": radio})


def _foreign_waypoint(srv, radio="!0badf00d"):
    event = _tcp_waypoint_event(srv, 920007)
    return ReceivedWaypointEvent(**{**event.__dict__, "local_radio_node_id": radio})


def test_a_text_from_another_radio_touches_nothing(srv, monkeypatch, capsys):
    signals = []
    monkeypatch.setattr(srv, "radio_event", lambda name, *a, **k: signals.append(name))

    outcome = inbound_events.ingest_received_text(_foreign_text(srv), srv._inbound_deps())

    assert outcome == inbound_events.STALE_RADIO
    assert _snapshot(srv) == {"messages": [], "chats": {}, "nodes": {}, "seen_ids": [], "seen_recent_texts": []}
    assert signals == [], "not even the radio-activity signal"
    out = capsys.readouterr().out
    assert "Dropped a received text from radio !0badf00d" in out and "from another radio" not in out, "no content in the log"
    assert inbound_events.get_inbound_stats()["stale_identity_dropped"] == 1


def test_a_waypoint_from_another_radio_is_not_stored(srv):
    outcome = inbound_events.ingest_received_waypoint(_foreign_waypoint(srv), srv._inbound_deps())

    assert outcome == inbound_events.STALE_RADIO and srv.waypoint_store.count() == 0


def test_events_buffered_before_a_profile_switch_are_dropped_after_it(srv, monkeypatch):
    """Radio A's events are queued; the accepted radio becomes B; then they drain."""
    queued = [_tcp_event(srv, _tcp_text_packet("for A", packet_id=1))]
    identity = dict(srv.INSTANCE_IDENTITY)
    identity["radio"] = {**identity.get("radio", {}), "node_id": "!b0b0b0b0"}
    monkeypatch.setattr(srv, "INSTANCE_IDENTITY", identity)
    monkeypatch.setattr(srv, "LOCAL_NODE_ID", "!b0b0b0b0")

    assert srv.active_radio_node_id() == "!b0b0b0b0"
    outcomes = [inbound_events.ingest_received_text(e, srv._inbound_deps()) for e in queued]

    assert outcomes == [inbound_events.STALE_RADIO] and srv.messages == []


def test_the_gate_compares_ids_case_and_whitespace_insensitively(srv):
    event = _tcp_event(srv, _tcp_text_packet("same radio"))
    shouted = ReceivedTextEvent(**{**event.__dict__, "local_radio_node_id": f" {event.local_radio_node_id.upper()} "})

    assert inbound_events.ingest_received_text(shouted, srv._inbound_deps()) == inbound_events.STORED


def test_the_serial_path_always_passes_its_own_gate(srv):
    srv.reset_inbound()
    srv._handle_listener_line(_text_line("serial hello"))

    assert len(srv.messages) == 1 and inbound_events.get_inbound_stats()["stale_identity_dropped"] == 0


def test_the_active_radio_id_follows_the_accepted_profile(srv, monkeypatch):
    identity = dict(srv.INSTANCE_IDENTITY)
    identity["radio"] = {**identity.get("radio", {}), "node_id": "!ABCDEF12"}
    monkeypatch.setattr(srv, "INSTANCE_IDENTITY", identity)

    assert srv.active_radio_node_id() == "!abcdef12"

    identity["radio"] = {**identity["radio"], "node_id": ""}
    monkeypatch.setattr(srv, "LOCAL_NODE_ID", "!11223344")
    assert srv.active_radio_node_id() == "!11223344", "falls back to the bootstrap value"


# ---------------------------------------------------------------------------
# NodeInfo parity (PR C) - process_nodeinfo() (serial) vs
# ingest_received_nodeinfo() (TCP), both built on _merge_nodeinfo_into_node().
#
# Position has NO serial counterpart at all (the CLI --listen path only ever
# saw a position bundled inside a "Received nodeinfo:" line, never standalone)
# so it has no parity section here - see
# test_inbound_events_nodeinfo_position_telemetry.py for its direct tests.
# ---------------------------------------------------------------------------


def _nodeinfo_line(*, node_id=REMOTE, long_name="Test Node", short_name="TST", hw_model="RAK4631", role="ROUTER",
                    rssi=-80, snr=5.5, hop_start=3, relay=240):
    """A NODEINFO_APP text buffer shaped like the CLI's own dict-ish output -
    the same shape tests/test_serial_nodeinfo_telemetry_characterization.py's
    own _block() uses, kept local here to avoid a cross-file import for one
    small helper."""
    parts = ["NODEINFO_APP", f"'id': '{node_id}'"]
    for key, value in (("longName", long_name), ("shortName", short_name), ("hwModel", hw_model), ("role", role)):
        if value is not None:
            parts.append(f"'{key}': '{value}'")
    for key, value in (("rxRssi", rssi), ("rxSnr", snr), ("hopStart", hop_start), ("relayNode", relay)):
        if value is not None:
            parts.append(f"'{key}': {value}")
    return "Received: {" + ", ".join(parts) + "}"


def _tcp_nodeinfo_packet(*, node_id=REMOTE, from_num=REMOTE_NUM, long_name="Test Node", short_name="TST",
                          hw_model="RAK4631", role="ROUTER", rssi=-80, snr=5.5, hop_start=3, relay=240, packet_id=201):
    user = {"id": node_id}
    for key, value in (("longName", long_name), ("shortName", short_name), ("hwModel", hw_model), ("role", role)):
        if value is not None:
            user[key] = value
    packet = {
        "from": from_num, "decoded": {"portnum": "NODEINFO_APP", "user": user}, "raw": object(),
    }
    for key, value in (("rxRssi", rssi), ("rxSnr", snr), ("hopStart", hop_start), ("relayNode", relay)):
        if value is not None:
            packet[key] = value
    if packet_id is not None:
        packet["id"] = packet_id
    return packet


def _tcp_nodeinfo_event(srv, packet):
    event = TCPTransport(host="192.168.2.34")._normalize_nodeinfo(packet, _interface(srv))
    assert isinstance(event, ReceivedNodeInfoEvent)
    return event


def _nodeinfo_via_serial(srv, lines):
    srv.reset_inbound()
    for line in lines:
        srv._handle_listener_line(line)
    return _snapshot(srv)


def _nodeinfo_via_tcp(srv, packets):
    srv.reset_inbound()
    outcomes = [inbound_events.ingest_received_nodeinfo(_tcp_nodeinfo_event(srv, p), srv._inbound_deps()) for p in packets]
    return _snapshot(srv), outcomes


def _assert_nodeinfo_parity(srv, lines, packets):
    serial = _nodeinfo_via_serial(srv, lines)
    tcp, outcomes = _nodeinfo_via_tcp(srv, packets)
    assert tcp == serial
    return serial, outcomes


def test_a_nodeinfo_produces_the_same_node(srv):
    snapshot, outcomes = _assert_nodeinfo_parity(srv, [_nodeinfo_line()], [_tcp_nodeinfo_packet()])

    assert outcomes == [inbound_events.STORED]
    node = snapshot["nodes"][REMOTE]
    assert node["name"] == "Test Node"
    assert (node["short_name"], node["hw_model"], node["role"]) == ("TST", "RAK4631", "ROUTER")
    assert (node["rssi"], node["snr"], node["hop_start"], node["relay_node"]) == ("-80", "5.5", "3", "240")
    assert REMOTE in snapshot["chats"]


def test_a_nodeinfo_with_no_name_fields_resets_the_name_on_both_paths(srv):
    """Pins the real, pre-existing quirk documented in
    _merge_nodeinfo_into_node()'s own docstring: unlike the text path, a
    nameless NODEINFO resets the display name on BOTH transports identically -
    not fixed by PR C, just no longer duplicated by it."""

    def prepare(s):
        s.nodes[REMOTE] = {"node_id": REMOTE, "name": "Already Named"}

    srv.reset_inbound()
    prepare(srv)
    # Called directly rather than through _handle_listener_line(): its own
    # has_nodeinfo buffering heuristic (unrelated to PR C - pre-existing
    # multi-line NODEINFO_APP collection logic) only treats a block as
    # complete when it mentions longName/shortName/hwModel/'user': at least
    # once, so a line naming NONE of them would just sit in the buffer
    # forever instead of reaching process_nodeinfo() at all.
    srv.process_nodeinfo(_nodeinfo_line(long_name=None, short_name=None, hw_model=None, role=None, rssi=None,
                                         snr=None, hop_start=None, relay=None))
    serial = _snapshot(srv)

    srv.reset_inbound()
    prepare(srv)
    inbound_events.ingest_received_nodeinfo(
        _tcp_nodeinfo_event(srv, _tcp_nodeinfo_packet(long_name=None, short_name=None, hw_model=None, role=None,
                                                       rssi=None, snr=None, hop_start=None, relay=None)),
        srv._inbound_deps(),
    )
    tcp = _snapshot(srv)

    assert tcp == serial
    assert serial["nodes"][REMOTE]["name"] == "Meshtastic 65f0"


def test_a_repeated_nodeinfo_never_erases_a_known_position_on_either_path(srv):
    def prepare(s):
        s.nodes[REMOTE] = {"node_id": REMOTE, "name": "Old", "position": {"latitude": 1.0, "longitude": 2.0}}

    srv.reset_inbound()
    prepare(srv)
    srv._handle_listener_line(_nodeinfo_line())
    serial = _snapshot(srv)

    srv.reset_inbound()
    prepare(srv)
    inbound_events.ingest_received_nodeinfo(_tcp_nodeinfo_event(srv, _tcp_nodeinfo_packet()), srv._inbound_deps())
    tcp = _snapshot(srv)

    assert tcp == serial
    assert tcp["nodes"][REMOTE]["position"] == {"latitude": 1.0, "longitude": 2.0}


def test_the_local_node_is_skipped_on_both_paths(srv):
    local = srv.active_radio_node_id()
    local_num = _local_num(srv)

    snapshot, outcomes = _assert_nodeinfo_parity(
        srv,
        [_nodeinfo_line(node_id=local)],
        [_tcp_nodeinfo_packet(node_id=local, from_num=local_num)],
    )
    assert outcomes == [inbound_events.SKIPPED_LOCAL]
    assert local not in snapshot["nodes"]


def _foreign_nodeinfo(srv, radio="!0badf00d"):
    event = _tcp_nodeinfo_event(srv, _tcp_nodeinfo_packet())
    return ReceivedNodeInfoEvent(**{**event.__dict__, "local_radio_node_id": radio})


def test_a_nodeinfo_from_another_radio_touches_nothing(srv):
    outcome = inbound_events.ingest_received_nodeinfo(_foreign_nodeinfo(srv), srv._inbound_deps())

    assert outcome == inbound_events.STALE_RADIO
    assert srv.nodes == {}


# ---------------------------------------------------------------------------
# Telemetry parity (PR C) - process_telemetry_line() (serial, unchanged) vs
# ingest_received_telemetry() (TCP), both reducing to the same, unmodified
# apply_node_telemetry(). `source` legitimately differs ("passive" vs "tcp"),
# asserted explicitly rather than stripped, the same way waypoint's
# `raw_packet` difference is handled above.
# ---------------------------------------------------------------------------


def _telemetry_line(*, variant="deviceMetrics", metrics, from_num=REMOTE_NUM, from_id=REMOTE):
    return (
        "Received: {'from': %d, 'decoded': {'portnum': 'TELEMETRY_APP', '%s': %r}, 'fromId': '%s'}"
        % (from_num, variant, metrics, from_id)
    )


def _tcp_telemetry_packet(*, variant="deviceMetrics", metrics, from_num=REMOTE_NUM, packet_id=202):
    packet = {
        "from": from_num, "decoded": {"portnum": "TELEMETRY_APP", "telemetry": {variant: metrics}}, "raw": object(),
    }
    if packet_id is not None:
        packet["id"] = packet_id
    return packet


def _tcp_telemetry_event(srv, packet):
    event = TCPTransport(host="192.168.2.34")._normalize_telemetry(packet, _interface(srv))
    assert isinstance(event, ReceivedTelemetryEvent)
    return event


@pytest.fixture
def no_telemetry_history(srv, monkeypatch):
    """Both paths call apply_node_telemetry(), which writes telemetry history
    for non-local nodes - stubbed so these tests compare node state only,
    same convention as test_serial_nodeinfo_telemetry_characterization.py's
    own telemetry_env fixture."""
    monkeypatch.setattr(srv.telemetry, "add_node_telemetry_record", lambda *a, **k: None)
    return srv


def _telemetry_via_serial(srv, lines):
    srv.reset_inbound()
    for line in lines:
        srv._handle_listener_line(line)
    return _snapshot(srv)


def _telemetry_via_tcp(srv, packets):
    srv.reset_inbound()
    outcomes = [inbound_events.ingest_received_telemetry(_tcp_telemetry_event(srv, p), srv._inbound_deps()) for p in packets]
    return _snapshot(srv), outcomes


@pytest.mark.parametrize("variant,metrics", [
    ("deviceMetrics", {"batteryLevel": 80, "voltage": 3.9}),
    ("environmentMetrics", {"temperature": 21.5, "relativeHumidity": 40.0}),
    ("powerMetrics", {"ch1Voltage": 4.8, "ch1Current": 0.6}),
])
def test_each_telemetry_variant_produces_the_same_node_state(no_telemetry_history, variant, metrics):
    srv = no_telemetry_history
    serial = _telemetry_via_serial(srv, [_telemetry_line(variant=variant, metrics=metrics)])
    tcp, outcomes = _telemetry_via_tcp(srv, [_tcp_telemetry_packet(variant=variant, metrics=metrics)])

    # `source` legitimately differs (serial: "passive", TCP: "tcp") - assert it
    # explicitly, then strip it before comparing the rest of the node state.
    serial_node, tcp_node = serial["nodes"][REMOTE], tcp["nodes"][REMOTE]
    assert serial_node["telemetry_source"] == "passive" and tcp_node["telemetry_source"] == "tcp"

    def _without_source(node):
        node = dict(node)
        node["telemetry_source"] = None
        node.pop("last_telemetry_time", None)
        node.pop("last_telemetry_time_text", None)
        for key in ("device_metrics", "environment_metrics", "power_metrics"):
            if isinstance(node.get(key), dict):
                inner = {k: v for k, v in node[key].items() if k not in ("source", "updated")}
                if isinstance(inner.get("channels"), dict):
                    inner["channels"] = {
                        cid: {k: v for k, v in ch.items() if k not in ("source", "updated")}
                        for cid, ch in inner["channels"].items()
                    }
                node[key] = inner
        return node

    assert _without_source(tcp_node) == _without_source(serial_node)
    assert outcomes == [inbound_events.STORED]


def test_telemetry_from_another_radio_touches_nothing(no_telemetry_history):
    srv = no_telemetry_history
    event = _tcp_telemetry_event(srv, _tcp_telemetry_packet(metrics={"batteryLevel": 80}))
    foreign = ReceivedTelemetryEvent(**{**event.__dict__, "local_radio_node_id": "!0badf00d"})

    outcome = inbound_events.ingest_received_telemetry(foreign, srv._inbound_deps())

    assert outcome == inbound_events.STALE_RADIO
    assert srv.nodes == {}
