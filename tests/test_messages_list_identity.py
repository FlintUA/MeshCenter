"""Audit review 2026-09-29, finding F2: `messages` is shared, long-lived
module state in server.py, handed out BY REFERENCE to api/api_chat.py's
register_chat_routes() DI call at import time (server.py:
`register_chat_routes(app, state_lock, chats, nodes, messages, ...)`).

clear_chat(), delete_chat() and delete_all_dm() all REBOUND the module-level
name (`messages = [m for m in messages if ...]`) instead of mutating the
existing list object in place. server.py's own global then points at a new
list, while api/api_chat.py's captured `messages` (used by `/api/messages`'s
global branch, `/api/messages/delete`, `/api/send/retry`) keeps pointing at
the OLD, now-disconnected one - no exception, just a silent desync for the
rest of the process's life. A message added after any of the three ran would
be invisible to api_chat.py's view: `/api/messages/delete` would wrongly
404 on a message that visibly exists in the chat.

Written BEFORE the fix - every test in this file fails on unmodified main.
`tests/conftest.py`'s `_reset_server_state` autouse fixture now also asserts
the same identity invariant generically after every test that touches
server_module, so a future regression of this exact kind fails immediately
without needing a dedicated test for it.
"""
import pytest

REMOTE = "!1fa065f0"
OTHER = "!2b3c4d5e"


@pytest.fixture
def srv(server_module):
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    yield server_module
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()


def _call(srv, endpoint, payload):
    with srv.app.test_request_context(json=payload):
        response = srv.app.view_functions[endpoint]()
    if isinstance(response, tuple):
        body, status = response[0], response[1]
    else:
        body, status = response, 200
    return body.get_json(), status


# ---------------------------------------------------------------------------
# Identity: the SAME list object must survive every handler
# ---------------------------------------------------------------------------


def test_clear_chat_mutates_the_messages_list_in_place(srv):
    srv.messages.append({"id": "keep", "chat_id": srv.CHANNEL_CHAT_ID, "text": "stays"})
    srv.messages.append({"id": "gone", "chat_id": REMOTE, "text": "cleared"})
    before = srv.messages

    body, status = _call(srv, "api_clear_chat", {"chat_id": REMOTE})

    assert status == 200 and body["ok"] is True
    assert srv.messages is before, "clear_chat() replaced the messages list instead of mutating it"
    assert [m["id"] for m in srv.messages] == ["keep"]


def test_delete_chat_mutates_the_messages_list_in_place(srv):
    srv.chats[REMOTE] = {"id": REMOTE, "name": "Remote"}
    srv.messages.append({"id": "keep", "chat_id": srv.CHANNEL_CHAT_ID, "text": "stays"})
    srv.messages.append({"id": "gone", "chat_id": REMOTE, "text": "cleared"})
    before = srv.messages

    body, status = _call(srv, "api_delete_chat", {"chat_id": REMOTE})

    assert status == 200 and body["ok"] is True
    assert srv.messages is before, "delete_chat() replaced the messages list instead of mutating it"
    assert [m["id"] for m in srv.messages] == ["keep"]


def test_delete_all_dm_mutates_the_messages_list_in_place(srv):
    srv.chats[REMOTE] = {"id": REMOTE, "name": "Remote"}
    srv.messages.append({"id": "keep", "chat_id": srv.CHANNEL_CHAT_ID, "text": "stays"})
    srv.messages.append({"id": "gone", "chat_id": REMOTE, "text": "cleared"})
    before = srv.messages

    body, status = _call(srv, "api_delete_all_dm", {})

    assert status == 200 and body["ok"] is True
    assert srv.messages is before, "delete_all_dm() replaced the messages list instead of mutating it"
    assert [m["id"] for m in srv.messages] == ["keep"]


# ---------------------------------------------------------------------------
# The real consequence: api_chat.py's own DI-captured `messages` desyncs
# ---------------------------------------------------------------------------


def test_a_message_added_after_clear_chat_can_still_be_deleted_via_the_api(srv):
    """The actual bug, not just the identity check: /api/messages/delete
    lives in api/api_chat.py, which captured `messages` by reference at
    server startup. If clear_chat() rebound the name, this handler keeps
    searching the OLD list - a message added AFTER the clear is invisible
    to it, so a perfectly normal delete wrongly 404s."""
    srv.messages.append({"id": "old", "chat_id": REMOTE, "text": "old message"})
    _call(srv, "api_clear_chat", {"chat_id": REMOTE})

    new_msg = srv.add_message("rx", "Someone", "arrived after the clear", node_id=REMOTE, chat_id=REMOTE)

    body, status = _call(srv, "api_delete_message", {"chat_id": REMOTE, "message_id": new_msg["id"]})

    assert status == 200 and body["ok"] is True, f"unexpected response: {body}"
    assert new_msg["id"] not in [m.get("id") for m in srv.messages]


def test_a_message_added_after_delete_chat_can_still_be_deleted_via_the_api(srv):
    srv.chats[REMOTE] = {"id": REMOTE, "name": "Remote"}
    srv.messages.append({"id": "old", "chat_id": REMOTE, "text": "old message"})
    _call(srv, "api_delete_chat", {"chat_id": REMOTE})

    new_msg = srv.add_message("rx", "Someone", "arrived after the delete", node_id=REMOTE, chat_id=REMOTE)

    body, status = _call(srv, "api_delete_message", {"chat_id": REMOTE, "message_id": new_msg["id"]})

    assert status == 200 and body["ok"] is True, f"unexpected response: {body}"


def test_a_message_added_after_delete_all_dm_can_still_be_deleted_via_the_api(srv):
    srv.chats[REMOTE] = {"id": REMOTE, "name": "Remote"}
    srv.messages.append({"id": "old", "chat_id": REMOTE, "text": "old message"})
    _call(srv, "api_delete_all_dm", {})

    new_msg = srv.add_message("rx", "Someone", "arrived after the wipe", node_id=REMOTE, chat_id=REMOTE)

    body, status = _call(srv, "api_delete_message", {"chat_id": REMOTE, "message_id": new_msg["id"]})

    assert status == 200 and body["ok"] is True, f"unexpected response: {body}"


# ---------------------------------------------------------------------------
# Existing behavior, unchanged: the actual filtering still works correctly
# ---------------------------------------------------------------------------


def test_clear_chat_only_removes_that_chats_own_messages(srv):
    srv.messages.append({"id": "a", "chat_id": REMOTE, "text": "remote"})
    srv.messages.append({"id": "b", "chat_id": OTHER, "text": "other"})
    srv.messages.append({"id": "c", "chat_id": srv.CHANNEL_CHAT_ID, "text": "channel"})

    _call(srv, "api_clear_chat", {"chat_id": REMOTE})

    assert sorted(m["id"] for m in srv.messages) == ["b", "c"]


def test_delete_all_dm_keeps_channel_messages_only(srv):
    srv.chats[REMOTE] = {"id": REMOTE, "name": "Remote"}
    srv.chats[OTHER] = {"id": OTHER, "name": "Other"}
    srv.messages.append({"id": "a", "chat_id": REMOTE, "text": "dm1"})
    srv.messages.append({"id": "b", "chat_id": OTHER, "text": "dm2"})
    srv.messages.append({"id": "c", "chat_id": srv.CHANNEL_CHAT_ID, "text": "channel"})
    srv.messages.append({"id": "d", "chat_id": "channel:1", "text": "secondary channel"})

    _call(srv, "api_delete_all_dm", {})

    assert sorted(m["id"] for m in srv.messages) == ["c", "d"]
    assert REMOTE not in srv.chats and OTHER not in srv.chats
