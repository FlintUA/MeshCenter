"""U1: every new message carries "ts" (epoch seconds); chats carry last_ts.
Old messages without ts must keep loading untouched."""
import json
import time

import pytest

REMOTE = "!1fa065f0"


@pytest.fixture
def srv(server_module):
    for d in (server_module.nodes, server_module.chats, server_module.messages):
        d.clear()
    yield server_module
    for d in (server_module.nodes, server_module.chats, server_module.messages):
        d.clear()


def test_resolve_message_ts_uses_sane_rx_time(srv):
    now = int(time.time())
    assert srv.resolve_message_ts(now - 120) == now - 120


@pytest.mark.parametrize("bad", [None, "", "x", 0, 1700000000, int(time.time()) + 86400 * 400])
def test_resolve_message_ts_falls_back_to_server_time(srv, bad):
    before = int(time.time())
    assert before <= srv.resolve_message_ts(bad) <= before + 2


def test_add_message_sets_ts_and_chat_last_ts(srv):
    rx = int(time.time()) - 30
    msg = srv.add_message("rx", "Remote", "hi", REMOTE, REMOTE, rx_time=rx)
    assert msg["ts"] == rx
    assert msg["time"]
    assert srv.chats[REMOTE]["last_ts"] == rx


def test_outgoing_message_gets_server_time(srv):
    before = int(time.time())
    msg = srv.add_message("me", "Me", "out", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID)
    assert before <= msg["ts"] <= before + 2


def test_legacy_messages_without_ts_still_load(srv, tmp_path, monkeypatch):
    legacy = tmp_path / "messages.json"
    legacy.write_text(json.dumps([{
        "id": "a", "kind": "rx", "sender": "R", "node_id": REMOTE,
        "text": "old", "time": "10:00:00", "chat_id": REMOTE, "chat_type": "dm",
    }]))
    monkeypatch.setattr(srv, "HISTORY_FILE", str(legacy))
    srv.load_messages()
    assert len(srv.messages) == 1
    assert "ts" not in srv.messages[0]
    assert srv.messages[0]["time"] == "10:00:00"


def test_chat_list_exposes_last_ts(srv):
    srv.add_message("rx", "Remote", "hi", REMOTE, REMOTE)
    chat_list, _ = srv.get_chats_list()
    entry = next(c for c in chat_list if c["id"] == REMOTE)
    assert isinstance(entry["last_ts"], int)


def test_chat_list_orders_by_last_ts_across_midnight(srv):
    a, b = "!1fa065f0", "!2b3c4d5e"
    yesterday_2300 = 1_800_000_000          # arbitrary epoch
    today_0900 = yesterday_2300 + 10 * 3600  # 10h later, "HH:MM:SS" string sorts lower
    srv.chats[a] = {"id": a, "name": "A", "type": "dm", "last_message": "x",
                    "last_time": "23:00:00", "last_ts": yesterday_2300, "unread": 0}
    srv.chats[b] = {"id": b, "name": "B", "type": "dm", "last_message": "y",
                    "last_time": "09:00:00", "last_ts": today_0900, "unread": 0}
    ids = [c["id"] for c in srv.get_chats_list()[0] if not c["is_channel"]]
    assert ids.index(a) < ids.index(b)  # same ascending order as before: older first
    # string order alone would have put B ("09:00:00") first
    assert "09:00:00" < "23:00:00"


def test_chat_list_legacy_chats_without_ts_still_sort(srv):
    a, b = "!1fa065f0", "!2b3c4d5e"
    srv.chats[a] = {"id": a, "name": "A", "type": "dm", "last_message": "", "last_time": "10:00:00", "unread": 0}
    srv.chats[b] = {"id": b, "name": "B", "type": "dm", "last_message": "", "last_time": "09:00:00", "last_ts": 1_800_000_000, "unread": 0}
    ids = [c["id"] for c in srv.get_chats_list()[0] if not c["is_channel"]]
    assert set([a, b]) <= set(ids)
