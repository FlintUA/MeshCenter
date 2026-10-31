"""MCA-UX1: MCAttach control frames in the chat history (server side)."""
from meshsrv import mca_chat
from meshsrv.attachments import codec


def frame(message_type):
    return codec.to_text(codec._canonical_cbor({0: codec.PROTOCOL_VERSION, 1: int(message_type)}))


OFFER_TEXT = frame(codec.MessageType.OFFER)
ACK_TEXT = frame(codec.MessageType.ACK_DOWNLOADED)


def test_marker_tells_an_offer_from_every_other_frame_and_ignores_plain_text():
    assert mca_chat.marker_for_text(OFFER_TEXT) == {"type": "OFFER"}
    assert mca_chat.marker_for_text(ACK_TEXT) == {"type": "CONTROL"}
    assert mca_chat.marker_for_text(frame(codec.MessageType.KEY_REQUEST)) == {"type": "CONTROL"}
    assert mca_chat.marker_for_text("MCA1:not-a-frame!!") == {"type": "CONTROL"}, "an undecodable frame is never shown raw"
    assert mca_chat.marker_for_text("hello") is None
    assert mca_chat.marker_for_text(None) is None


def test_unread_and_preview_rules():
    assert mca_chat.counts_as_unread(None) and mca_chat.counts_as_unread({"type": "OFFER"})
    assert not mca_chat.counts_as_unread({"type": "CONTROL"})
    assert mca_chat.preview_text(None, "hi") == "hi"
    assert mca_chat.preview_text({"type": "CONTROL"}, "MCA1:x") is None, "control frames never replace the preview"
    assert mca_chat.preview_text({"type": "OFFER"}, "MCA1:x") == "\U0001F4CE"


def test_ensure_marker_adds_it_to_old_messages_once_and_never_to_own_messages():
    old = {"kind": "rx", "text": ACK_TEXT}
    assert mca_chat.ensure_marker(old)["mca"] == {"type": "CONTROL"}
    own = {"kind": "me", "text": OFFER_TEXT}
    assert "mca" not in mca_chat.ensure_marker(own)
    plain = {"kind": "rx", "text": "hello"}
    assert "mca" not in mca_chat.ensure_marker(plain)


def test_add_message_marks_frames_and_keeps_control_frames_out_of_unread_and_preview(server_module):
    s = server_module
    node = "!aaaa1111"
    with s.state_lock:
        s.messages.clear()
        s.chats.pop(node, None)
    first = s.add_message("rx", "Peer", "hello there", node_id=node, chat_id=node)
    assert "mca" not in first
    unread_before = s.chats[node]["unread"]

    control = s.add_message("rx", "Peer", ACK_TEXT, node_id=node, chat_id=node)
    assert control["mca"] == {"type": "CONTROL"}
    assert s.chats[node]["unread"] == unread_before, "a control frame is not an unread message"
    assert s.chats[node]["last_message"] == "hello there", "and not the chat list preview"

    offer = s.add_message("rx", "Peer", OFFER_TEXT, node_id=node, chat_id=node)
    assert offer["mca"] == {"type": "OFFER"}
    assert s.chats[node]["unread"] == unread_before + 1
    assert s.chats[node]["last_message"] == "\U0001F4CE"


def test_a_queued_transfer_is_noted_as_one_sent_bubble(server_module):
    s = server_module
    node = "!aaaa2222"
    with s.state_lock:
        s.chats.pop(node, None)
    s._note_transfer_in_chat(node, "att123")
    sent = [m for m in s.messages if m.get("chat_id") == node][-1]
    assert sent["kind"] == "me" and sent["mca"] == {"type": "FILE_SENT", "attachment_id": "att123"}


def test_old_stored_frames_are_marked_when_the_history_is_read(server_module):
    s = server_module
    node = "!aaaa3333"
    with s.state_lock:
        s.chats.pop(node, None)
    s.add_message("rx", "Peer", "hello", node_id=node, chat_id=node)
    with s.state_lock:
        s.messages.append({"id": "legacy1", "kind": "rx", "sender": "Peer", "node_id": node, "text": OFFER_TEXT,
                           "time": "10:00", "ts": 1, "chat_id": node, "chat_type": "dm", "chat_name": "Peer", "status": "sent"})
    history = s.get_chat_messages(node)
    legacy = [m for m in history if m["id"] == "legacy1"][0]
    assert legacy["mca"] == {"type": "OFFER"}


def test_the_page_is_never_served_from_a_stale_cache(server_module):
    response = server_module.app.test_client().get("/")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-cache"
