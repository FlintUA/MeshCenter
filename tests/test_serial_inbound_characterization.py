"""Characterization of the SERIAL inbound path: `--listen` stdout line ->
_handle_listener_line() -> nodes / chats / messages / seen_ids / waypoints.db.

Written BEFORE the shared-ingest refactor, against the old code, and required to
pass unchanged after it: this is the regression net for moving the tail of
_handle_listener_line() into meshsrv/inbound_events.py. Every assertion below
records what the serial path did at the time; none was bent to fit the refactor.

Lines are shaped like the Meshtastic CLI's `Received: {...}` dict output.
"""
import json
import re

import pytest

REMOTE = "!1fa065f0"
REMOTE_NUM = 0x1FA065F0
OTHER = "!2b3c4d5e"
OTHER_NUM = 0x2B3C4D5E


def _text_line(text="hello mesh", *, from_num=REMOTE_NUM, from_id=REMOTE, to_num=4294967295, to_id="^all",
               packet_id=101, channel=None, rssi=-80, snr=5.5, hop_start=3, relay=240, reply_id=None):
    parts = [
        f"'from': {from_num}", f"'to': {to_num}",
    ]
    if channel is not None:
        parts.append(f"'channel': {channel}")
    decoded = f"'portnum': 'TEXT_MESSAGE_APP', 'payload': b'x', 'bitfield': 1, 'text': '{text}'"
    if reply_id:
        decoded += f", 'replyId': {reply_id}"
    parts.append(f"'decoded': {{{decoded}}}")
    if packet_id is not None:
        parts.append(f"'id': {packet_id}")
    parts += [
        "'rxTime': 1790455378", f"'rxSnr': {snr}", "'hopLimit': 3", f"'rxRssi': {rssi}",
        f"'hopStart': {hop_start}", f"'relayNode': {relay}", "'raw': from: 1", f"'fromId': '{from_id}'", f"'toId': '{to_id}'",
    ]
    return "Received: {" + ", ".join(parts) + "}"


def _waypoint_line(*, waypoint_id, name="Cafe", description="meet here", lat_i=504501000, lon_i=305234000,
                   icon=128205, expire=4102444800, from_id=REMOTE, packet_id=102, channel=1):
    return (
        "Received: {'from': %d, 'to': 4294967295, 'channel': %d, 'decoded': {'portnum': 'WAYPOINT_APP', "
        "'payload': b'y', 'waypoint': {'id': %d, 'latitudeI': %d, 'longitudeI': %d, 'expire': %d, 'name': '%s', "
        "'description': '%s', 'icon': %d, 'raw': id: 1}}, 'id': %d, 'fromId': '%s', 'toId': '^all'}"
        % (REMOTE_NUM, channel, waypoint_id, lat_i, lon_i, expire, name, description, icon, packet_id, from_id)
    )


def _messages(server):
    return [
        {k: v for k, v in m.items() if k not in ("id", "time")} for m in server.messages
    ]


@pytest.fixture
def srv(server_module):
    """server_module with a clean inbound state (the autouse fixture restores it afterwards)."""
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()
    store = server_module.waypoint_store
    store.delete_all()
    yield server_module
    store.delete_all()


def _feed(srv, line):
    srv._handle_listener_line(line)


# --- channel / DM text -------------------------------------------------------


def test_primary_channel_text_creates_message_node_and_unread(srv):
    _feed(srv, _text_line())

    [message] = _messages(srv)
    assert message["kind"] == "rx"
    assert message["node_id"] == REMOTE
    assert message["text"] == "hello mesh"
    assert message["chat_id"] == srv.CHANNEL_CHAT_ID and message["chat_type"] == "channel"
    assert message["packet_id"] == 101
    assert message["status"] == "sent"
    assert "reply_to" not in message or message["reply_to"] is None

    assert srv.chats[srv.CHANNEL_CHAT_ID]["unread"] == 1
    assert srv.chats[srv.CHANNEL_CHAT_ID]["last_message"] == "hello mesh"
    assert 101 in srv.seen_ids


def test_the_sending_node_is_recorded_with_string_signal_values(srv):
    _feed(srv, _text_line(rssi=-80, snr=5.5, hop_start=3, relay=240))

    node = srv.nodes[REMOTE]
    assert (node["rssi"], node["snr"], node["hop_start"], node["relay_node"]) == ("-80", "5.5", "3", "240")
    assert node["last_text"] == "hello mesh"
    assert node["node_id"] == REMOTE and node["ignored"] is False and node["favorite"] is False
    assert node["role"] == "CLIENT" and node["position"] is None
    assert node["last_seen"] > 0
    assert REMOTE in srv.chats, "a DM chat is opened for the node too"


def test_a_secondary_channel_gets_its_own_chat(srv):
    _feed(srv, _text_line("on ch2", channel=2))

    [message] = _messages(srv)
    assert message["chat_id"] == "channel:2" and message["chat_type"] == "channel"
    assert srv.chats["channel:2"] == {
        "id": "channel:2", "name": "Channel 2", "type": "channel",
        "last_message": "on ch2", "last_time": srv.chats["channel:2"]["last_time"], "unread": 1,
    }


def test_channel_index_is_clamped_to_0_7(srv):
    _feed(srv, _text_line("high", channel=12))

    assert _messages(srv)[0]["chat_id"] == "channel:7"


def test_a_direct_message_goes_to_the_senders_dm_chat(srv):
    _feed(srv, _text_line("psst", to_num=0x756F9960, to_id="!756f9960"))

    [message] = _messages(srv)
    assert message["chat_id"] == REMOTE and message["chat_type"] == "dm"
    assert message["node_id"] == REMOTE
    assert srv.chats[REMOTE]["unread"] == 1 and srv.chats[REMOTE]["last_message"] == "psst"
    assert srv.chats[srv.CHANNEL_CHAT_ID]["unread"] == 0 if srv.CHANNEL_CHAT_ID in srv.chats else True


def test_unicode_text_is_stored_exactly(srv):
    _feed(srv, _text_line("Привет TCP 👋"))

    assert _messages(srv)[0]["text"] == "Привет TCP 👋"


def test_a_line_that_is_not_a_text_message_creates_nothing(srv):
    _feed(srv, "Received: {'from': 1, 'decoded': {'portnum': 'POSITION_APP'}, 'id': 9}")
    _feed(srv, "some unrelated log line")
    _feed(srv, "")

    assert srv.messages == [] and len(srv.seen_ids) == 0


def test_empty_text_is_ignored(srv):
    _feed(srv, _text_line(""))

    assert srv.messages == []


# --- duplicates ---------------------------------------------------------------


def test_the_same_packet_id_is_stored_once_and_does_not_touch_the_node_again(srv):
    _feed(srv, _text_line("first", packet_id=7, rssi=-80))
    _feed(srv, _text_line("first", packet_id=7, rssi=-42))

    assert len(_messages(srv)) == 1
    assert srv.nodes[REMOTE]["rssi"] == "-80", "a packet-id duplicate is dropped BEFORE the node update"
    assert srv.chats[srv.CHANNEL_CHAT_ID]["unread"] == 1


def test_the_same_text_within_15s_is_stored_once_but_the_node_is_still_updated(srv):
    _feed(srv, _text_line("same words", packet_id=1, rssi=-80))
    _feed(srv, _text_line("same words", packet_id=2, rssi=-42))

    assert len(_messages(srv)) == 1
    assert srv.nodes[REMOTE]["rssi"] == "-42", "the text-duplicate check runs AFTER update_node"
    assert {1, 2} <= set(srv.seen_ids)


def test_without_a_packet_id_the_text_fallback_still_dedups(srv):
    _feed(srv, _text_line("no id", packet_id=None))
    _feed(srv, _text_line("no id", packet_id=None))

    assert len(_messages(srv)) == 1
    assert len(srv.seen_ids) == 0
    assert "packet_id" not in _messages(srv)[0]


def test_different_texts_from_the_same_node_are_both_kept(srv):
    _feed(srv, _text_line("one", packet_id=1))
    _feed(srv, _text_line("two", packet_id=2))

    assert [m["text"] for m in _messages(srv)] == ["one", "two"]
    assert srv.chats[srv.CHANNEL_CHAT_ID]["unread"] == 2


def test_the_same_text_from_two_nodes_is_not_a_duplicate(srv):
    _feed(srv, _text_line("hi all", packet_id=1))
    _feed(srv, _text_line("hi all", packet_id=2, from_num=OTHER_NUM, from_id=OTHER))

    assert [m["node_id"] for m in _messages(srv)] == [REMOTE, OTHER]


# --- ignored / local / replies / MCA --------------------------------------


def test_an_ignored_node_updates_but_stores_nothing(srv):
    srv.nodes[REMOTE] = {"node_id": REMOTE, "name": "Muted", "ignored": True}
    _feed(srv, _text_line("spam", rssi=-11))

    assert srv.messages == []
    assert srv.nodes[REMOTE]["rssi"] == "-11" and srv.nodes[REMOTE]["ignored"] is True


def test_a_message_from_the_local_node_skips_the_node_update(srv):
    local = srv.LOCAL_NODE_ID
    num = int(local[1:], 16)
    _feed(srv, _text_line("echo", from_num=num, from_id=local))

    assert local not in srv.nodes, "SKIP_LOCAL_NODE: the local node is never written by a text packet"
    [message] = _messages(srv)
    assert message["chat_id"] == srv.CHANNEL_CHAT_ID


def test_a_reply_carries_a_reference_to_the_original(srv):
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)
    original = srv.messages[-1]

    _feed(srv, _text_line("the answer", packet_id=8, reply_id=555))

    reply = srv.messages[-1]
    assert reply["reply_to"] == srv.build_reply_reference(original)
    assert reply["reply_to"]["packet_id"] == 555


def test_a_reply_to_an_unknown_packet_has_no_reference(srv):
    _feed(srv, _text_line("orphan reply", packet_id=8, reply_id=999999))

    assert srv.messages[-1].get("reply_to") in (None, {})


@pytest.fixture
def mca_calls(srv, monkeypatch):
    calls = []
    monkeypatch.setattr(
        srv.mca_runtime, "handle_incoming_meshtastic_text",
        lambda text, node_id, router, **kwargs: calls.append((text, node_id, router, kwargs)),
    )
    return calls


def test_a_direct_mca1_text_is_dispatched_to_mcattach(srv, mca_calls):
    _feed(srv, _text_line("MCA1:abc", to_num=0x756F9960, to_id="!756f9960", packet_id=31, channel=1))

    assert len(mca_calls) == 1
    text, node_id, router, kwargs = mca_calls[0]
    assert (text, node_id) == ("MCA1:abc", REMOTE)
    assert router is srv.transport_router
    assert kwargs["packet_id"] == 31 and kwargs["channel_index"] == 1 and kwargs["data_dir"] == srv.DATA_DIR
    assert srv.messages[-1]["text"] == "MCA1:abc", "the message is saved as usual, MCA is on top of that"


def test_mca_channel_index_is_none_when_the_line_has_no_channel(srv, mca_calls):
    _feed(srv, _text_line("MCA1:abc", to_num=0x756F9960, to_id="!756f9960", packet_id=32, channel=None))

    assert mca_calls[0][3]["channel_index"] is None


def test_a_broadcast_mca1_text_is_not_dispatched(srv, mca_calls):
    _feed(srv, _text_line("MCA1:abc", packet_id=33))

    assert mca_calls == []
    assert srv.messages[-1]["text"] == "MCA1:abc"


def test_an_mca_dispatch_failure_does_not_lose_the_message(srv, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("mca down")

    monkeypatch.setattr(srv.mca_runtime, "handle_incoming_meshtastic_text", boom)
    _feed(srv, _text_line("MCA1:abc", to_num=0x756F9960, to_id="!756f9960", packet_id=34))

    assert srv.messages[-1]["text"] == "MCA1:abc"


def test_radio_event_text_and_packet_are_signalled(srv, monkeypatch):
    seen = []
    real = srv.radio_event
    monkeypatch.setattr(srv, "radio_event", lambda name, *a, **k: (seen.append(name), real(name, *a, **k))[1])

    _feed(srv, _text_line("ping", packet_id=77))

    # H1-C2: cli_activity now fires for every line (the old unconditional
    # "packet" signal, renamed); "packet" itself now only fires for a
    # recognized received-packet line like this one.
    assert seen == ["cli_activity", "packet", "text"]
    _feed(srv, _text_line("ping", packet_id=77))  # duplicate: text is signalled BEFORE the dedup
    assert seen == ["cli_activity", "packet", "text", "cli_activity", "packet", "text"]


# --- waypoints ----------------------------------------------------------------


def test_a_waypoint_line_is_stored(srv):
    line = _waypoint_line(waypoint_id=910001)
    _feed(srv, line)

    row = srv.waypoint_store.get(910001)
    assert row["sender_id"] == REMOTE
    assert (row["name"], row["description"]) == ("Cafe", "meet here")
    assert row["latitude"] == pytest.approx(50.4501) and row["longitude"] == pytest.approx(30.5234)
    assert (row["icon"], row["expire_at"], row["channel_index"]) == (128205, 4102444800, 1)
    assert row["is_active"] == 1
    assert row["raw_packet"] == line, "the serial path keeps the CLI line for diagnostics"


def test_a_repeated_identical_waypoint_is_a_duplicate(srv):
    line = _waypoint_line(waypoint_id=910002)
    _feed(srv, line)
    first = dict(srv.waypoint_store.get(910002))
    _feed(srv, line)

    assert srv.waypoint_store.count() == 1
    assert srv.waypoint_store.get(910002)["name"] == first["name"]


def test_a_changed_waypoint_with_the_same_id_is_an_update_not_a_second_row(srv):
    _feed(srv, _waypoint_line(waypoint_id=910003, name="Cafe"))
    _feed(srv, _waypoint_line(waypoint_id=910003, name="Bar", lat_i=505000000))

    assert srv.waypoint_store.count() == 1
    row = srv.waypoint_store.get(910003)
    assert row["name"] == "Bar" and row["latitude"] == pytest.approx(50.5)


def test_waypoint_events_are_logged(srv, monkeypatch, capsys):
    events = []
    monkeypatch.setattr(srv, "log_system_event", lambda **kw: events.append(kw))
    _feed(srv, _waypoint_line(waypoint_id=910004))
    _feed(srv, _waypoint_line(waypoint_id=910004))  # duplicate: silent
    _feed(srv, _waypoint_line(waypoint_id=910004, name="Renamed"))

    assert [e["title"] for e in events] == ["Waypoint created", "Waypoint updated"]
    assert events[0]["source"] == "waypoint" and events[0]["level"] == "INFO"
    assert "Cafe" in events[0]["details"] and "50.4501" in events[0]["details"]
    out = capsys.readouterr().out
    assert "[WAYPOINT] Received: Cafe" in out and "[WAYPOINT] Updated: Renamed" in out


def test_a_waypoint_line_without_coordinates_is_not_stored(srv):
    line = ("Received: {'from': %d, 'decoded': {'portnum': 'WAYPOINT_APP', 'waypoint': {'id': 910005, 'expire': 0}}, "
            "'id': 5, 'fromId': '%s'}" % (REMOTE_NUM, REMOTE))
    _feed(srv, line)

    assert srv.waypoint_store.count() == 0


def test_text_and_waypoint_lines_do_not_interfere(srv):
    _feed(srv, _waypoint_line(waypoint_id=910006))
    _feed(srv, _text_line("hello"))

    assert srv.waypoint_store.count() == 1 and len(_messages(srv)) == 1
