"""P2: marking a chat read is an explicit, CSRF-protected, persisted POST.
GET /api/messages is read-only (it used to zero `unread` and rewrite
chats.json - unchecked - on every 2 s poll)."""
import pytest

CHAT = "!11223344"
CHANNEL = "channel:1"


@pytest.fixture
def srv(server_module):
    for d in (server_module.nodes, server_module.chats, server_module.messages):
        d.clear()
    server_module.chats[CHAT] = {"id": CHAT, "name": "N", "type": "dm", "last_message": "hi",
                                 "last_time": "t", "unread": 3}
    server_module.chats[CHANNEL] = {"id": CHANNEL, "name": "C", "type": "channel", "last_message": "",
                                    "last_time": "", "unread": 0}
    yield server_module
    for d in (server_module.nodes, server_module.chats, server_module.messages):
        d.clear()


@pytest.fixture
def writes(srv, monkeypatch):
    """Counts (and optionally fails) every chats.json write."""
    state = {"count": 0, "fail": False}
    real = srv.safe_write_json

    def spy(path, data, *a, **k):
        if path == srv.CHATS_FILE:
            state["count"] += 1
            if state["fail"]:
                return False
        return real(path, data, *a, **k)

    monkeypatch.setattr(srv, "safe_write_json", spy)
    return state


def _client(srv, token="tok"):
    client = srv.app.test_client()
    with client.session_transaction() as sess:
        sess["csrf_token"] = token
    return client


def _post(client, payload, token="tok"):
    headers = {"X-CSRF-Token": token} if token else {}
    return client.post("/api/messages/mark_read", json=payload, headers=headers)


# ---- GET is read-only ---------------------------------------------------------

def test_get_messages_for_a_chat_does_not_change_unread_or_write(srv, writes):
    res = _client(srv).get(f"/api/messages?chat_id={CHAT}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["chat_info"]["unread"] == 3          # the true state, not a reset one
    assert srv.chats[CHAT]["unread"] == 3
    assert writes["count"] == 0


def test_get_messages_without_chat_id_does_not_write(srv, writes):
    res = _client(srv).get("/api/messages")
    assert res.status_code == 200
    assert writes["count"] == 0 and srv.chats[CHAT]["unread"] == 3


def test_repeated_polls_never_write(srv, writes):
    client = _client(srv)
    for _ in range(5):
        client.get(f"/api/messages?chat_id={CHAT}")
    assert writes["count"] == 0


# ---- POST mark_read -----------------------------------------------------------

def test_post_without_csrf_token_is_rejected(srv, writes):
    res = _post(_client(srv), {"chat_id": CHAT}, token=None)
    assert res.status_code == 403
    assert res.get_json()["error_code"] == "csrf_invalid"
    assert srv.chats[CHAT]["unread"] == 3 and writes["count"] == 0


def test_post_with_a_wrong_token_is_rejected(srv):
    res = _post(_client(srv), {"chat_id": CHAT}, token="not-the-token")
    assert res.status_code == 403
    assert srv.chats[CHAT]["unread"] == 3


def test_post_marks_read_and_it_is_persisted(srv, writes):
    res = _post(_client(srv), {"chat_id": CHAT})
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "changed": True, "unread": 0}
    assert writes["count"] == 1
    srv.load_chats()                                   # what a restart would read back
    assert srv.chats[CHAT]["unread"] == 0


def test_post_with_a_failing_write_is_500_and_restores_the_count(srv, writes):
    writes["fail"] = True
    res = _post(_client(srv), {"chat_id": CHAT})
    assert res.status_code == 500
    body = res.get_json()
    assert body["ok"] is False and body["error_code"] == "storage_write_failed"
    assert srv.chats[CHAT]["unread"] == 3              # memory still matches disk


def test_post_when_already_read_is_a_no_op_without_a_write(srv, writes):
    res = _post(_client(srv), {"chat_id": CHANNEL})    # unread is 0
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "changed": False}
    assert writes["count"] == 0


def test_post_for_an_unknown_chat_is_a_no_op_without_a_write(srv, writes):
    res = _post(_client(srv), {"chat_id": "!99999999"})
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "changed": False}
    assert writes["count"] == 0


@pytest.mark.parametrize("payload", [{}, {"chat_id": ""}, {"chat_id": "../etc/passwd"}, {"chat_id": 5}, {"chat_id": "!zz"}])
def test_post_with_an_invalid_chat_id_is_400(srv, writes, payload):
    res = _post(_client(srv), payload)
    assert res.status_code == 400
    assert res.get_json()["error_code"] == "invalid_chat_id"
    assert writes["count"] == 0


def test_post_with_a_malformed_body_is_400(srv):
    client = _client(srv)
    res = client.post("/api/messages/mark_read", data="{not json", content_type="application/json",
                      headers={"X-CSRF-Token": "tok"})
    assert res.status_code == 400


# ---- the send path ------------------------------------------------------------

def test_reset_unread_on_send_never_raises_on_a_failed_write(srv, writes, monkeypatch):
    events = []
    monkeypatch.setattr(srv, "log_system_event",
                        lambda title, level="INFO", details="", source="system": events.append((title, level)))
    writes["fail"] = True
    assert srv.reset_unread(CHAT) is False             # the send is not failed by this
    assert srv.chats[CHAT]["unread"] == 3
    assert ("Could not save chat read state", "WARNING") in events


def test_reset_unread_on_send_persists_when_the_write_works(srv, writes):
    assert srv.reset_unread(CHAT) is True
    assert srv.chats[CHAT]["unread"] == 0 and writes["count"] == 1
    assert srv.reset_unread(CHAT) is True              # already 0: no second write
    assert writes["count"] == 1


def test_mark_chat_read_helper_contract(srv, writes):
    assert srv.mark_chat_read("!99999999") == (False, True)
    assert srv.mark_chat_read(CHANNEL) == (False, True)
    writes["fail"] = True
    assert srv.mark_chat_read(CHAT) == (False, False)
    assert srv.chats[CHAT]["unread"] == 3
    writes["fail"] = False
    assert srv.mark_chat_read(CHAT) == (True, True)
