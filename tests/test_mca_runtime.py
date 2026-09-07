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

import time
import uuid

from meshsrv.attachments import codec
from meshsrv.attachments import mca_runtime
from meshsrv.attachments.delivery.fakes import FakeRadioTransport, InMemoryEther
from meshsrv.attachments.identity import load_signing_key
from meshsrv.attachments.service import AttachmentsService


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


# ---- ADR-0008: OFFER routing and AttachmentsService startup wiring --------


def test_offer_from_unknown_provider_is_routed_to_receiver_not_dropped(tmp_path):
    """Before ADR-0008's wiring, `handle_incoming_meshtastic_text()` only
    ever reached `KeyExchangeCoordinator`, which returns None for an
    OFFER (it only dispatches KEY_REQUEST/KEY_ANNOUNCE/KEY_ACK) - a real
    incoming OFFER was silently dropped with no attachment row ever
    created. This proves the fix through the exact same glue function
    server.py's listener calls, encoding a real, signed OFFER through
    the real `MeshtasticTextAdapter`/`codec` - not by calling
    `receiver.handle_offer()` directly."""
    mca_runtime.reset_state_for_tests()
    try:
        ether = InMemoryEther()
        transport_a = FakeRadioTransport(ether, "!aaaaaaaa")
        transport_b = FakeRadioTransport(ether, "!bbbbbbbb")
        data_dir_a = str(tmp_path / "a")
        data_dir_b = str(tmp_path / "b")

        from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter

        adapter_a = MeshtasticTextAdapter(transport_a)
        state_a = mca_runtime._get_state(data_dir_a)  # noqa: SLF001
        signing_key_a = load_signing_key(state_a.workspace_manager, state_a.principal)

        offer = codec.encode_offer(
            codec.OfferFields(
                provider_id=b"\x01\x02\x03\x04\x05\x06\x07\x08",
                transfer_id=uuid.uuid4().bytes,
                sender_key_id=bytes.fromhex(state_a.principal.key_id),
                kind=1,
                size_bucket=1,
                hard_expires_at=int(time.time()) + 3600,
                flags=0,
            ),
            signing_key_a,
        )
        route = adapter_a.resolve_route({"node_id": "!bbbbbbbb"})
        wire_payload = adapter_a.encode(offer, route)
        adapter_a.send(wire_payload, route, idempotency_key="test-offer")

        events = ether.drain("!bbbbbbbb")
        assert len(events) == 1
        recognized = mca_runtime.handle_incoming_meshtastic_text(
            events[0]["text"], events[0]["source_address"], transport_b,
            data_dir=data_dir_b, packet_id=events[0]["packet_id"],
        )
        assert recognized is True

        # No binding is known for sender_key_id yet (WAITING_KEY takes
        # priority over the provider check - receiver.py's own state
        # machine only evaluates the provider once the sender's identity
        # is known) - per Step 1.5's own DoD, this must create exactly
        # one non-terminal row and send zero network requests (there is
        # no HTTP client wired into this test at all, so a network
        # attempt would raise, not just fail an assertion).
        state_b = mca_runtime._get_state(data_dir_b)  # noqa: SLF001
        rows = state_b.conn.execute("SELECT state FROM attachments").fetchall()
        assert [r[0] for r in rows] == ["WAITING_KEY"]
    finally:
        mca_runtime.reset_state_for_tests()


def test_offer_wakes_the_attachments_service_when_one_is_running(tmp_path, monkeypatch):
    """If `start_attachments_service()` has already run, an inbound OFFER
    must call `service.wake()` so the worker re-scans promptly instead of
    waiting out its full `tick_seconds` interval."""
    mca_runtime.reset_state_for_tests()
    try:
        ether = InMemoryEther()
        transport_a = FakeRadioTransport(ether, "!aaaaaaaa")
        transport_b = FakeRadioTransport(ether, "!bbbbbbbb")
        data_dir_a = str(tmp_path / "a")
        data_dir_b = str(tmp_path / "b")

        from meshsrv.attachments.delivery.meshtastic import MeshtasticTextAdapter

        adapter_a = MeshtasticTextAdapter(transport_a)
        state_a = mca_runtime._get_state(data_dir_a)  # noqa: SLF001
        signing_key_a = load_signing_key(state_a.workspace_manager, state_a.principal)

        mca_runtime.start_attachments_service(data_dir_b, transport_b)
        state_b = mca_runtime._get_state(data_dir_b)  # noqa: SLF001
        assert isinstance(state_b.service, AttachmentsService)

        woken = []
        monkeypatch.setattr(state_b.service, "wake", lambda: woken.append(True))

        offer = codec.encode_offer(
            codec.OfferFields(
                provider_id=b"\x01\x02\x03\x04\x05\x06\x07\x08",
                transfer_id=uuid.uuid4().bytes,
                sender_key_id=bytes.fromhex(state_a.principal.key_id),
                kind=1,
                size_bucket=1,
                hard_expires_at=int(time.time()) + 3600,
                flags=0,
            ),
            signing_key_a,
        )
        route = adapter_a.resolve_route({"node_id": "!bbbbbbbb"})
        wire_payload = adapter_a.encode(offer, route)
        adapter_a.send(wire_payload, route, idempotency_key="test-offer-wake")

        events = ether.drain("!bbbbbbbb")
        mca_runtime.handle_incoming_meshtastic_text(
            events[0]["text"], events[0]["source_address"], transport_b,
            data_dir=data_dir_b, packet_id=events[0]["packet_id"],
        )
        assert woken == [True]
    finally:
        mca_runtime.reset_state_for_tests()


def test_start_attachments_service_is_idempotent(tmp_path):
    mca_runtime.reset_state_for_tests()
    try:
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!cccccccc")
        data_dir = str(tmp_path / "c")

        mca_runtime.start_attachments_service(data_dir, transport)
        state = mca_runtime._get_state(data_dir)  # noqa: SLF001
        first_service = state.service
        assert isinstance(first_service, AttachmentsService)

        mca_runtime.start_attachments_service(data_dir, transport)
        assert state.service is first_service
    finally:
        mca_runtime.reset_state_for_tests()


def test_attachments_service_shares_the_runtime_lock_not_a_private_one(tmp_path):
    """Regression coverage for a reviewer-found defect (PR #227 defect
    #2): AttachmentsService used to always build its own private
    threading.Lock(), independent of mca_runtime's own module-level
    `_lock` - the lock handle_incoming_meshtastic_text() holds for every
    direct write it makes to the exact same `state.conn`. Two
    independent locks over one shared sqlite3.Connection is not mutual
    exclusion: the radio listener thread and the service's own worker
    thread could freely interleave statements/commits on that one
    connection. ensure_service() must now hand its own `_lock` to
    AttachmentsService's constructor, so both sides serialize on the
    exact same lock object - this pins that wiring directly rather than
    trying to provoke and detect an actual race (inherently flaky)."""
    mca_runtime.reset_state_for_tests()
    try:
        ether = InMemoryEther()
        transport = FakeRadioTransport(ether, "!dddddddd")
        data_dir = str(tmp_path / "d")

        mca_runtime.start_attachments_service(data_dir, transport)
        state = mca_runtime._get_state(data_dir)  # noqa: SLF001
        assert isinstance(state.service, AttachmentsService)

        assert state.service._lock is mca_runtime._lock  # noqa: SLF001
    finally:
        mca_runtime.reset_state_for_tests()
