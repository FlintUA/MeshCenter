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


# ---- the offer bubble links to the real attachment ------------------------------------------------------------


def _real_offer_text(transfer_id):
    from nacl.signing import SigningKey
    key = SigningKey.generate()
    raw = codec.encode_offer(
        codec.OfferFields(provider_id=b"p" * 8, transfer_id=transfer_id, sender_key_id=b"k" * 8, kind=0,
                          size_bucket=1, hard_expires_at=2_145_916_800, flags=0),
        key,
    )
    return codec.to_text(raw)


def test_an_offer_marker_carries_the_transfer_id():
    marker = mca_chat.marker_for_text(_real_offer_text(b"" * 16))
    assert marker == {"type": "OFFER", "transfer_id": ("07" * 16)}


def test_link_attachment_writes_the_id_into_the_matching_offer_only():
    tid_a, tid_b = "aa" * 16, "bb" * 16
    messages = [
        {"id": "1", "mca": {"type": "OFFER", "transfer_id": tid_a}},
        {"id": "2", "mca": {"type": "OFFER", "transfer_id": tid_b}},
        {"id": "3", "text": "hello"},
    ]
    assert mca_chat.link_attachment(messages, tid_b, "att-b") is True
    assert messages[1]["mca"]["attachment_id"] == "att-b"
    assert "attachment_id" not in messages[0]["mca"]
    assert mca_chat.link_attachment(messages, tid_b, "att-b") is False, "idempotent"
    assert mca_chat.link_attachment(messages, "cc" * 16, "att-c") is False, "an unknown transfer changes nothing"


def test_server_links_an_offer_bubble_when_the_handler_reports_the_attachment(server_module):
    s = server_module
    node = "!aaaa4444"
    tid = b"	" * 16
    with s.state_lock:
        s.chats.pop(node, None)
    offer = s.add_message("rx", "Peer", _real_offer_text(tid), node_id=node, chat_id=node)
    assert offer["mca"]["transfer_id"] == tid.hex() and "attachment_id" not in offer["mca"]
    s._link_offer_to_chat(tid.hex(), "att-real")
    history = [m for m in s.get_chat_messages(node) if m["id"] == offer["id"]][0]
    assert history["mca"]["attachment_id"] == "att-real"


# ---- MCA-UX2: list previews never show a raw frame ------------------------------------------------------------------


def test_chat_list_preview_comes_from_the_newest_visible_message(server_module):
    s = server_module
    node = "!aaaa5555"
    with s.state_lock:
        s.chats.pop(node, None)
        s.messages[:] = [m for m in s.messages if m.get("chat_id") != node]
    s.add_message("rx", "Peer", "see you at nine", node_id=node, chat_id=node)
    s.add_message("rx", "Peer", ACK_TEXT, node_id=node, chat_id=node)          # a control frame is the newest message
    chat = [c for c in s.get_chats_list()[0] if c["id"] == node][0]
    assert chat["last_message"] == "see you at nine", "the control frame is skipped"


def test_chat_list_preview_repairs_a_stored_raw_frame_preview(server_module):
    s = server_module
    node = "!aaaa6666"
    with s.state_lock:
        s.chats.pop(node, None)
        s.messages[:] = [m for m in s.messages if m.get("chat_id") != node]
    s.add_message("rx", "Peer", "hello", node_id=node, chat_id=node)
    with s.state_lock:
        s.chats[node]["last_message"] = OFFER_TEXT[:100]          # what an older version stored
        s.messages.append({"id": "old-offer", "kind": "rx", "sender": "Peer", "node_id": node, "text": OFFER_TEXT,
                           "time": "10:00", "ts": 5, "chat_id": node, "chat_type": "dm", "chat_name": "Peer", "status": "sent"})
    chat = [c for c in s.get_chats_list()[0] if c["id"] == node][0]
    assert chat["last_message"] == "\U0001F4CE" and not chat["last_message"].startswith("MCA1:")


def test_a_chat_with_only_control_frames_has_an_empty_preview(server_module):
    s = server_module
    node = "!aaaa7777"
    with s.state_lock:
        s.chats.pop(node, None)
        s.messages[:] = [m for m in s.messages if m.get("chat_id") != node]
    s.add_message("rx", "Peer", ACK_TEXT, node_id=node, chat_id=node)
    chat = [c for c in s.get_chats_list()[0] if c["id"] == node][0]
    assert chat["last_message"] == ""
