"""Reply Metadata Consistency (PR 2, execution plan) - the architectural
fix. `reply_id` (the protocol fact "this is a reply to packet X") is now
stored independently of `reply_to` (an optional, best-effort local
enrichment): an inbound reply whose original can't be resolved locally no
longer loses every trace that it was ever a reply.

Covers the plan's Tests 1-8 plus direct unit coverage of the three new/
changed pieces: add_message()'s new reply_id parameter,
find_reply_original() (the single ambiguity-guarded lookup ingest-time and
projection-time resolution both share), and resolve_reply_reference()
(lazy, projection-time re-resolution - server.py's get_chat_messages()).

Serial/TCP parity for reply_id itself (already int|None on both transports,
protected by _plain_int_or_none_if_zero()/extract_reply_id()/int() casts)
is characterized in tests/test_serial_inbound_characterization.py and
tests/test_inbound_parity.py; this file is additive to those, not a
replacement - the two pre-existing characterization tests there
(test_a_reply_carries_a_reference_to_the_original,
test_a_reply_to_an_unknown_packet_has_no_reference) are required to keep
passing unchanged.
"""
import pytest

from meshsrv import inbound_events
from test_serial_inbound_characterization import _text_line
from test_inbound_parity import _tcp_event, _tcp_text_packet

REMOTE = "!1fa065f0"
OTHER = "!2b3c4d5e"


@pytest.fixture
def srv(server_module):
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()
    inbound_events.reset_inbound_stats()
    yield server_module
    server_module.nodes.clear()
    server_module.chats.clear()
    server_module.messages.clear()
    server_module.seen_ids.clear()
    server_module.seen_recent_texts.clear()


# ---------------------------------------------------------------------------
# add_message(): reply_id is independent protocol metadata
# ---------------------------------------------------------------------------


def test_add_message_persists_reply_id_even_without_a_resolved_reply_to(srv):
    msg = srv.add_message("rx", "Someone", "orphan reply", node_id=REMOTE, reply_id=999999, reply_to=None)

    assert msg["reply_id"] == 999999
    assert "reply_to" not in msg


def test_add_message_persists_both_when_reply_to_resolves(srv):
    original = srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)
    reply_to = srv.build_reply_reference(original)

    msg = srv.add_message("rx", "Someone", "the answer", node_id=REMOTE, reply_id=555, reply_to=reply_to)

    assert msg["reply_id"] == 555
    assert msg["reply_to"]["packet_id"] == 555


def test_add_message_without_reply_id_or_reply_to_has_neither_key(srv):
    msg = srv.add_message("rx", "Someone", "just a message", node_id=REMOTE)

    assert "reply_id" not in msg and "reply_to" not in msg


def test_add_message_falls_back_to_reply_tos_own_packet_id_for_the_outbound_path(srv):
    """The outbound /api/send path (api/api_chat.py) never passes reply_id
    explicitly - it always already has a fully-resolved reply_to from the
    frontend's own local data. add_message() fills reply_id from
    reply_to["packet_id"] itself so outbound replies get the same schema,
    without api_chat.py needing to change (plan section 15: outbound path
    untouched)."""
    reply_to = {"packet_id": 42, "id": "x", "sender": "Remote", "text": "hi", "chat_id": "channel"}

    msg = srv.add_message("me", "Me", "my reply", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, reply_to=reply_to)

    assert msg["reply_id"] == 42


def test_add_message_reply_id_is_cast_to_int_and_ignores_garbage(srv):
    msg = srv.add_message("rx", "Someone", "x", node_id=REMOTE, reply_id="77")
    assert msg["reply_id"] == 77

    msg2 = srv.add_message("rx", "Someone", "y", node_id=REMOTE, reply_id="not-a-number")
    assert "reply_id" not in msg2


# ---------------------------------------------------------------------------
# find_reply_original(): the ambiguity-guarded lookup
# ---------------------------------------------------------------------------


def test_find_reply_original_trusts_a_same_chat_match(srv):
    srv.add_message("me", "Me", "in channel", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=100)

    found = srv.find_reply_original(100, srv.CHANNEL_CHAT_ID)

    assert found is not None and found["text"] == "in channel"


def test_find_reply_original_resolves_an_unambiguous_global_match(srv):
    """The reply arrived in a DM chat that has never seen this packet_id
    itself, but exactly one other chat has - unambiguous, so it resolves."""
    srv.add_message("me", "Me", "in channel", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=200)

    found = srv.find_reply_original(200, REMOTE)

    assert found is not None and found["text"] == "in channel"


def test_find_reply_original_refuses_a_cross_chat_ambiguous_match(srv):
    """Section 13: the same packet_id legitimately recurring across chats is
    a real protocol possibility (10-bit per-boot counter + 22 random bits;
    firmware dedup is a bounded window, not forever), not just defensive
    paranoia - guessing which chat a reply meant would risk quoting the
    wrong message."""
    srv.add_message("me", "Me", "in channel", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=300)
    srv.add_message("rx", "Someone", "in a dm", node_id=REMOTE, chat_id=REMOTE, packet_id=300)

    found = srv.find_reply_original(300, OTHER)

    assert found is None, "ambiguous across two chats - must not guess"


def test_find_reply_original_same_chat_match_is_trusted_even_if_ambiguous_elsewhere(srv):
    """Section 12: chat-local lookup always comes first and is authoritative
    - a same-chat match short-circuits before the cross-chat ambiguity
    check ever runs."""
    srv.add_message("me", "Me", "in channel", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=400)
    srv.add_message("rx", "Someone", "in a dm too", node_id=REMOTE, chat_id=REMOTE, packet_id=400)

    found = srv.find_reply_original(400, srv.CHANNEL_CHAT_ID)

    assert found is not None and found["text"] == "in channel"


def test_find_reply_original_with_no_match_anywhere_is_none(srv):
    assert srv.find_reply_original(123456789, srv.CHANNEL_CHAT_ID) is None


def test_find_reply_original_handles_legacy_string_packet_ids(srv):
    """Test 7 (regression): packet_id type normalization is already
    protected in three places (the TCP adapter, extract_reply_id(), and
    find_message_by_packet_id()'s own int() casts on both sides) - confirm
    find_reply_original() inherits that discipline rather than adding an
    unguarded comparison of its own."""
    srv.messages.append({"id": "legacy", "chat_id": REMOTE, "packet_id": "77", "text": "old record", "sender": "X"})

    assert srv.find_reply_original(77, srv.CHANNEL_CHAT_ID)["text"] == "old record"
    assert srv.find_reply_original("77", srv.CHANNEL_CHAT_ID)["text"] == "old record"


# ---------------------------------------------------------------------------
# resolve_reply_reference(): lazy, projection-time enrichment
# ---------------------------------------------------------------------------


def test_resolve_reply_reference_passes_through_an_already_resolved_message(srv, monkeypatch):
    monkeypatch.setattr(srv, "find_reply_original", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-lookup")))
    msg = {"reply_id": 1, "reply_to": {"packet_id": 1, "text": "already resolved"}}

    assert srv.resolve_reply_reference(msg) is msg


def test_resolve_reply_reference_passes_through_a_plain_message(srv):
    msg = {"text": "just a message"}

    assert srv.resolve_reply_reference(msg) is msg


def test_resolve_reply_reference_resolves_a_reply_id_when_the_original_is_now_known(srv):
    """Test 3: the original was missing when the reply first arrived
    (reply_id persisted, reply_to left None); once the original later
    appears in history, the NEXT read resolves it - no re-ingest needed."""
    unresolved = {"chat_id": REMOTE, "reply_id": 500}
    assert srv.resolve_reply_reference(unresolved) is unresolved, "not yet known - stays as-is"

    srv.add_message("rx", "Someone", "the original, arriving late", node_id=REMOTE, chat_id=REMOTE, packet_id=500)

    enriched = srv.resolve_reply_reference(unresolved)
    assert enriched is not unresolved, "a new dict, the input is never mutated"
    assert enriched["reply_to"]["text"] == "the original, arriving late"
    assert unresolved.get("reply_to") is None, "the original message object was not mutated"


def test_resolve_reply_reference_stays_unresolved_when_still_ambiguous(srv):
    msg = {"chat_id": OTHER, "reply_id": 600}
    srv.add_message("me", "Me", "channel copy", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=600)
    srv.add_message("rx", "Someone", "dm copy", node_id=REMOTE, chat_id=REMOTE, packet_id=600)

    assert srv.resolve_reply_reference(msg) is msg


def test_get_chat_messages_applies_lazy_resolution(srv):
    srv.messages.append({"id": "a", "chat_id": REMOTE, "reply_id": 700, "text": "orphan reply", "sender": "X"})

    assert srv.get_chat_messages(REMOTE)[0].get("reply_to") is None

    srv.add_message("rx", "Someone", "the original", node_id=REMOTE, chat_id=REMOTE, packet_id=700)

    [reply] = [m for m in srv.get_chat_messages(REMOTE) if m["id"] == "a"]
    assert reply["reply_to"]["text"] == "the original"


# ---------------------------------------------------------------------------
# End-to-end via ingest_received_text() - Tests 1, 2, 4, 5, 6, 8
# ---------------------------------------------------------------------------


def test_1_resolved_reply_end_to_end_serial(srv):
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)

    srv._handle_listener_line(_text_line("the answer", packet_id=8, reply_id=555))

    reply = srv.messages[-1]
    assert reply["reply_id"] == 555
    assert reply["reply_to"]["packet_id"] == 555


def test_2_unresolved_reply_end_to_end_serial_keeps_reply_id(srv):
    srv._handle_listener_line(_text_line("orphan reply", packet_id=8, reply_id=999999))

    reply = srv.messages[-1]
    assert reply["reply_id"] == 999999
    assert "reply_to" not in reply, "no local original - reply_to stays unresolved, not fabricated"


def test_4_no_reply_end_to_end_is_unchanged(srv):
    srv._handle_listener_line(_text_line("just a message", packet_id=9))

    assert "reply_id" not in srv.messages[-1] and "reply_to" not in srv.messages[-1]


def test_5_serial_parity_reply_id_persists(srv):
    srv._handle_listener_line(_text_line("reply", packet_id=8, reply_id=77))

    assert srv.messages[-1]["reply_id"] == 77


def test_6_tcp_parity_reply_id_persists(srv):
    event = _tcp_event(srv, _tcp_text_packet("reply", packet_id=8, reply_id=77))

    inbound_events.ingest_received_text(event, srv._inbound_deps())

    assert srv.messages[-1]["reply_id"] == 77


def _strip_volatile(message):
    stripped = {k: v for k, v in message.items() if k not in ("id", "time", "ts")}
    if isinstance(stripped.get("reply_to"), dict):
        stripped["reply_to"] = {k: v for k, v in stripped["reply_to"].items() if k != "id"}
    return stripped


def test_serial_and_tcp_give_identical_reply_semantics_for_the_same_logical_event(srv):
    """Section 14: one logical inbound reply -> the same persisted
    reply_id/reply_to shape regardless of transport."""
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)
    srv._handle_listener_line(_text_line("the answer", packet_id=8, reply_id=555))
    serial_reply = _strip_volatile(srv.messages[-1])

    srv.messages.clear()
    srv.seen_ids.clear()
    srv.seen_recent_texts.clear()
    srv.add_message("me", "Me", "the question", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=555)
    event = _tcp_event(srv, _tcp_text_packet("the answer", packet_id=8, reply_id=555))
    inbound_events.ingest_received_text(event, srv._inbound_deps())
    tcp_reply = _strip_volatile(srv.messages[-1])

    assert serial_reply == tcp_reply


def test_8_cross_chat_ambiguity_stays_unresolved_end_to_end(srv):
    """Test 8 (not an edge case - see find_reply_original()'s own
    docstring): the same packet_id exists in two OTHER chats, and the reply
    itself arrives in a third chat that has no match of its own - a
    same-chat match always wins first (section 12), so the reply must land
    somewhere other than either candidate for this to actually exercise the
    ambiguity guard rather than the trusted same-chat path."""
    srv.add_message("me", "Me", "channel original", srv.LOCAL_NODE_ID, srv.CHANNEL_CHAT_ID, packet_id=321)
    srv.add_message("rx", "Someone", "dm original", node_id=OTHER, chat_id=OTHER, packet_id=321)

    srv._handle_listener_line(_text_line("which one?", packet_id=9, reply_id=321, channel=2))

    reply = srv.messages[-1]
    assert reply["chat_id"] == "channel:2", "sanity check: lands in neither candidate chat"
    assert reply["reply_id"] == 321
    assert "reply_to" not in reply, "ambiguous - stays unresolved rather than guessing"
