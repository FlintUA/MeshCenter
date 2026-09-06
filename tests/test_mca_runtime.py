"""tests/test_mca_runtime.py

Execution Plan Step 1.3: end-to-end test of
meshsrv.attachments.mca_runtime.handle_incoming_meshtastic_text() - the
actual glue server.py's radio listener calls - through two
FakeRadioTransport-backed nodes sharing one InMemoryEther. No real
hardware needed; this is what proves the *wiring* is correct before
tests/hardware/test_meshtastic_delivery_adapter_live.py proves the same
protocol survives a real radio link.
"""

from __future__ import annotations

from meshsrv.attachments import codec
from meshsrv.attachments import mca_runtime
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther


def test_key_request_to_key_announce_round_trip_through_the_real_glue(tmp_path):
    """Node A sends a real KEY_REQUEST (encoded/sent through
    MeshtasticTextAdapter, exactly as a UI action would); Node B's
    listener hook (handle_incoming_meshtastic_text) recognizes it,
    dispatches it through the real KeyExchangeCoordinator, and sends
    back a real, signed KEY_ANNOUNCE - which Node A's own listener hook
    then also recognizes."""
    mca_runtime.reset_state_for_tests()
    try:
        ether = InMemoryEther()
        transport_a = FakeRadioTransport(ether, "!aaaaaaaa")
        transport_b = FakeRadioTransport(ether, "!bbbbbbbb")
        data_dir_a = str(tmp_path / "a")
        data_dir_b = str(tmp_path / "b")

        # Node A builds and sends a real KEY_REQUEST the same way a UI
        # action would - not through the runtime glue (a KEY_REQUEST is
        # something a human triggers, not something arrives-and-gets-
        # auto-replied-to on the sending side).
        from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter
        from meshsrv.attachments.identity import load_signing_key

        adapter_a = MeshtasticTextAdapter(transport_a)
        state_a = mca_runtime._get_state(data_dir_a)  # noqa: SLF001 - test needs the sender's own principal/signing key
        signing_key_a = load_signing_key(state_a.workspace_manager, state_a.principal)
        key_request = codec.encode_key_request(
            codec.KeyRequestFields(sender_key_id=bytes.fromhex(state_a.principal.key_id)),
            signing_key_a,
        )
        route = adapter_a.resolve_route({"node_id": "!bbbbbbbb"})
        wire_payload = adapter_a.encode(key_request, route)
        adapter_a.send(wire_payload, route, idempotency_key="test-key-request")

        # Node B's listener "receives" it - this is the exact call
        # server.py's process_message_line() makes after saving the
        # incoming message.
        events = ether.drain("!bbbbbbbb")
        assert len(events) == 1
        recognized = mca_runtime.handle_incoming_meshtastic_text(
            events[0]["text"],
            events[0]["source_address"],
            transport_b,
            data_dir=data_dir_b,
            packet_id=events[0]["packet_id"],
        )
        assert recognized is True

        # Node B's coordinator should have sent a real KEY_ANNOUNCE back.
        reply_events = ether.drain("!aaaaaaaa")
        assert len(reply_events) == 1
        assert reply_events[0]["text"].startswith(codec.TEXT_PREFIX)
        announce_logical = codec.from_text(reply_events[0]["text"])
        assert codec.peek_message_type(announce_logical) == codec.MessageType.KEY_ANNOUNCE

        # Node A's own listener hook recognizes and processes the
        # KEY_ANNOUNCE too (records the binding, replies with KEY_ACK) -
        # proving the full round trip works from both sides through the
        # same glue function.
        recognized_a = mca_runtime.handle_incoming_meshtastic_text(
            reply_events[0]["text"],
            reply_events[0]["source_address"],
            transport_a,
            data_dir=data_dir_a,
            packet_id=reply_events[0]["packet_id"],
        )
        assert recognized_a is True

        ack_events = ether.drain("!bbbbbbbb")
        assert len(ack_events) == 1
        ack_logical = codec.from_text(ack_events[0]["text"])
        assert codec.peek_message_type(ack_logical) == codec.MessageType.KEY_ACK
    finally:
        mca_runtime.reset_state_for_tests()


def test_ordinary_chat_message_is_not_recognized_as_mca(tmp_path):
    mca_runtime.reset_state_for_tests()
    try:
        ether = InMemoryEther()
        transport_b = FakeRadioTransport(ether, "!bbbbbbbb")
        recognized = mca_runtime.handle_incoming_meshtastic_text(
            "hey, got your message",
            "!aaaaaaaa",
            transport_b,
            data_dir=str(tmp_path / "b"),
        )
        assert recognized is False
        assert ether.drain("!aaaaaaaa") == []
    finally:
        mca_runtime.reset_state_for_tests()
