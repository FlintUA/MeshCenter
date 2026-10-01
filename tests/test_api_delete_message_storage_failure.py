"""F4.1 PR 2 (review fix): /api/messages/delete (api/api_chat.py) used to
call save_messages()/save_chats() without even checking their return
value - the only node/chat mutator in the review's list that hadn't been
touched at all. It now checks both, answers 500 storage_write_failed on
failure, and restores the popped message plus the chat's summary fields to
exactly what they held before the request (in place - the F2 identity
invariant covered by tests/test_messages_list_identity.py).
"""
import pytest


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


CHAT_ID = "!11223344"


def _seed(srv):
    srv.chats[CHAT_ID] = {
        "id": CHAT_ID, "name": "N", "type": "dm",
        "last_message": "second", "last_time": "t2", "unread": 5,
    }
    srv.messages.append({"id": "m1", "chat_id": CHAT_ID, "text": "first", "time": "t1"})
    srv.messages.append({"id": "m2", "chat_id": CHAT_ID, "text": "second", "time": "t2"})


def test_delete_message_failed_messages_write_restores_message_and_chat_entry(srv, monkeypatch):
    _seed(srv)
    original_messages = list(srv.messages)
    original_chat_entry = dict(srv.chats[CHAT_ID])

    monkeypatch.setattr(srv, "safe_write_json", lambda *a, **k: False)

    data, status = _call(srv, "api_delete_message", {"chat_id": CHAT_ID, "message_id": "m2"})

    assert status == 500
    assert data["error_code"] == "storage_write_failed"
    assert srv.messages == original_messages
    assert srv.chats[CHAT_ID] == original_chat_entry


def test_delete_message_failed_chats_write_restores_message_and_chat_entry(srv, monkeypatch):
    _seed(srv)
    original_messages = list(srv.messages)
    original_chat_entry = dict(srv.chats[CHAT_ID])

    real_safe_write_json = srv.safe_write_json

    def _fail_only_chats(path, data):
        if path == srv.CHATS_FILE:
            return False
        return real_safe_write_json(path, data)

    monkeypatch.setattr(srv, "safe_write_json", _fail_only_chats)

    data, status = _call(srv, "api_delete_message", {"chat_id": CHAT_ID, "message_id": "m2"})

    assert status == 500
    assert data["error_code"] == "storage_write_failed"
    assert srv.messages == original_messages
    assert srv.chats[CHAT_ID] == original_chat_entry


def test_delete_message_success_still_works(srv):
    _seed(srv)

    data, status = _call(srv, "api_delete_message", {"chat_id": CHAT_ID, "message_id": "m2"})

    assert status == 200
    assert data["ok"] is True
    assert [m["id"] for m in srv.messages] == ["m1"]
    assert srv.chats[CHAT_ID]["last_message"] == "first"
    assert srv.chats[CHAT_ID]["unread"] == 0
