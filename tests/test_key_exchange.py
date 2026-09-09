"""tests/test_key_exchange.py

Execution Plan Step 1.2: KEY_REQUEST/KEY_ANNOUNCE/KEY_ACK protocol logic
(design spec sections 7.2-7.4) and the rate limiter, driven entirely
through the transport-neutral `DeliveryAdapter` contract - see
key_exchange.py's module docstring for why that satisfies this step's
"real Meshtastic direct messages via the existing AdapterIPCTransport"
DoD without importing AdapterIPCTransport here.
"""

from __future__ import annotations

import sqlite3

import pytest

from meshsrv.attachments import codec
from meshsrv.attachments.db.migrations import migrate
from meshsrv.attachments.delivery.base import DeliveryEnvelope, RouteType, WireFormat
from meshsrv.attachments.delivery.fakes import FakeTextAdapter, InMemoryEther
from meshsrv.attachments.identity import create_principal
from meshsrv.attachments.key_exchange import (
    AddressStatus,
    KeyExchangeCoordinator,
    KeyExchangeError,
    RateLimited,
)
from meshsrv.attachments.workspace import MCAWorkspaceManager


def _make_node(tmp_path, name, now_fn):
    db_dir = tmp_path / name
    db_dir.mkdir()
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    workspace_manager = MCAWorkspaceManager(db_dir)
    principal = create_principal(conn, workspace_manager, f"ws-{name}", now=now_fn())
    coordinator = KeyExchangeCoordinator(conn, workspace_manager, principal, "fake-text", now_fn=now_fn)
    return conn, workspace_manager, principal, coordinator


def _direct_envelope(logical_message: bytes, source_address: str, now: float) -> DeliveryEnvelope:
    return DeliveryEnvelope(
        logical_message=logical_message,
        wire_format=WireFormat.MCA1_TEXT,
        adapter_id="fake-text",
        connector_profile_id="fake-connector",
        route_type=RouteType.DIRECT,
        route_id=source_address,
        source_address=source_address,
        external_message_id=None,
        received_at=now,
    )


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return _Clock()


def test_key_request_over_direct_route_triggers_signed_announce(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, principal, coordinator = _make_node(tmp_path, "a", clock)
    # Build a real KEY_REQUEST from a second, throwaway identity so the
    # signature is well-formed (decode_key_request is not asked to verify
    # it - see key_exchange.py - but it must still parse as valid CBOR).
    requester_key = SigningKey.generate()
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), requester_key)
    envelope = _direct_envelope(request, "!requester", clock())

    reply = coordinator.handle_incoming(envelope)

    assert reply is not None
    announce = codec.decode_key_announce(reply, verify_against_self=True)
    assert announce.public_identity == principal.public_identity
    assert announce.epoch == principal.epoch


def test_broadcast_or_channel_key_request_is_ignored(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())
    for route_type in (RouteType.CHANNEL, RouteType.CHAT, RouteType.MANUAL):
        envelope = DeliveryEnvelope(
            logical_message=request,
            wire_format=WireFormat.MCA1_TEXT,
            adapter_id="fake-text",
            connector_profile_id="fake-connector",
            route_type=route_type,
            route_id="some-channel",
            source_address="!requester",
            external_message_id=None,
            received_at=clock(),
        )
        assert coordinator.handle_incoming(envelope) is None


def test_second_request_from_same_address_within_ten_minutes_is_rate_limited(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())

    first = coordinator.handle_incoming(_direct_envelope(request, "!requester", clock()))
    assert first is not None

    clock.advance(60)  # only 1 minute later - inside the 10-minute gate
    with pytest.raises(RateLimited):
        coordinator.handle_incoming(_direct_envelope(request, "!requester", clock()))


def test_request_from_same_address_after_ten_minutes_is_allowed_again(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())

    coordinator.handle_incoming(_direct_envelope(request, "!requester", clock()))
    clock.advance(10 * 60 + 1)
    second = coordinator.handle_incoming(_direct_envelope(request, "!requester", clock()))
    assert second is not None


def test_hourly_quota_blocks_the_thirteenth_distinct_address_in_one_hour(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())

    for i in range(12):
        reply = coordinator.handle_incoming(_direct_envelope(request, f"!node{i}", clock()))
        assert reply is not None
        clock.advance(1)  # distinct addresses, so the per-address gate never fires

    with pytest.raises(RateLimited):
        coordinator.handle_incoming(_direct_envelope(request, "!node-13th", clock()))


def test_hourly_quota_resets_after_the_window_passes(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())

    for i in range(12):
        coordinator.handle_incoming(_direct_envelope(request, f"!node{i}", clock()))
        clock.advance(1)

    clock.advance(3600)
    reply = coordinator.handle_incoming(_direct_envelope(request, "!node-after-reset", clock()))
    assert reply is not None


def test_rate_limit_state_survives_a_new_coordinator_instance(tmp_path, clock):
    """Spec 7.4: 'ограничения переживают перезапуск' - modeled here as a
    fresh KeyExchangeCoordinator over the same (already-migrated)
    connection, standing in for a process restart."""
    from nacl.signing import SigningKey

    conn, workspace_manager, principal, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())
    coordinator.handle_incoming(_direct_envelope(request, "!requester", clock()))

    restarted = KeyExchangeCoordinator(conn, workspace_manager, principal, "fake-text", now_fn=clock)
    clock.advance(60)
    with pytest.raises(RateLimited):
        restarted.handle_incoming(_direct_envelope(request, "!requester", clock()))


def test_force_announce_bypasses_rate_limit(tmp_path, clock):
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())
    coordinator.handle_incoming(_direct_envelope(request, "!requester", clock()))
    clock.advance(1)
    reply = coordinator.force_announce("!requester")
    assert codec.decode_key_announce(reply, verify_against_self=True) is not None


# ---- KEY_ANNOUNCE / TOFU / key-change handling --------------------------


def test_unknown_address_status_before_any_announce(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    assert coordinator.get_status("!stranger") == AddressStatus.KEY_UNKNOWN


def test_receiving_key_announce_creates_unconfirmed_binding_and_replies_key_ack(tmp_path, clock):
    from meshsrv.attachments.identity import load_signing_key
    from meshsrv.attachments.workspace import MCAWorkspaceManager as _WM

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, _, principal_b, _ = _make_node(tmp_path, "b", clock)

    wm_b = _WM(tmp_path / "b")
    signing_key_b = load_signing_key(wm_b, principal_b)
    announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=principal_b.public_identity, epoch=0), signing_key_b
    )

    reply = coordinator_a.handle_incoming(_direct_envelope(announce, "!node-b", clock()))

    assert reply is not None
    codec.decode_key_ack(reply, verify_key=None)
    assert coordinator_a.get_status("!node-b") == AddressStatus.KEY_UNVERIFIED
    binding = coordinator_a.get_binding("!node-b")
    assert binding.public_identity == principal_b.public_identity
    assert binding.tofu_confirmed_at is None


def test_confirm_tofu_makes_status_ready(tmp_path, clock):
    from meshsrv.attachments.identity import load_signing_key
    from meshsrv.attachments.workspace import MCAWorkspaceManager as _WM

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, _, principal_b, _ = _make_node(tmp_path, "b", clock)
    signing_key_b = load_signing_key(_WM(tmp_path / "b"), principal_b)
    announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=principal_b.public_identity, epoch=0), signing_key_b
    )
    coordinator_a.handle_incoming(_direct_envelope(announce, "!node-b", clock()))

    coordinator_a.confirm_tofu("!node-b")

    assert coordinator_a.get_status("!node-b") == AddressStatus.MCA_READY


def test_key_change_after_tofu_confirmation_is_parked_not_applied(tmp_path, clock):
    from nacl.signing import SigningKey

    from meshsrv.attachments.identity import load_signing_key
    from meshsrv.attachments.workspace import MCAWorkspaceManager as _WM

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, _, principal_b, _ = _make_node(tmp_path, "b", clock)
    signing_key_b = load_signing_key(_WM(tmp_path / "b"), principal_b)
    first_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=principal_b.public_identity, epoch=0), signing_key_b
    )
    coordinator_a.handle_incoming(_direct_envelope(first_announce, "!node-b", clock()))
    coordinator_a.confirm_tofu("!node-b")

    # Someone (attacker, or node-b legitimately reinstalled) now announces
    # a completely different key from the same transport address.
    impostor_key = SigningKey.generate()
    impostor_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=bytes(impostor_key.verify_key), epoch=0), impostor_key
    )
    coordinator_a.handle_incoming(_direct_envelope(impostor_announce, "!node-b", clock()))

    # The trusted binding must be untouched.
    binding = coordinator_a.get_binding("!node-b")
    assert binding.public_identity == principal_b.public_identity
    assert binding.tofu_confirmed_at is not None
    assert coordinator_a.get_status("!node-b") == AddressStatus.KEY_CHANGED
    assert binding.pending_public_identity == bytes(impostor_key.verify_key)


def test_accept_pending_key_change_promotes_and_requires_fresh_tofu(tmp_path, clock):
    from nacl.signing import SigningKey

    from meshsrv.attachments.identity import load_signing_key
    from meshsrv.attachments.workspace import MCAWorkspaceManager as _WM

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, _, principal_b, _ = _make_node(tmp_path, "b", clock)
    signing_key_b = load_signing_key(_WM(tmp_path / "b"), principal_b)
    first_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=principal_b.public_identity, epoch=0), signing_key_b
    )
    coordinator_a.handle_incoming(_direct_envelope(first_announce, "!node-b", clock()))
    coordinator_a.confirm_tofu("!node-b")

    new_key = SigningKey.generate()
    new_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=bytes(new_key.verify_key), epoch=1), new_key
    )
    coordinator_a.handle_incoming(_direct_envelope(new_announce, "!node-b", clock()))

    coordinator_a.accept_pending_key_change("!node-b")

    binding = coordinator_a.get_binding("!node-b")
    assert binding.public_identity == bytes(new_key.verify_key)
    assert binding.tofu_confirmed_at is None  # fresh TOFU required, not inherited
    assert coordinator_a.get_status("!node-b") == AddressStatus.KEY_UNVERIFIED


def test_accept_pending_key_change_without_a_pending_change_raises(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    with pytest.raises(KeyExchangeError):
        coordinator.accept_pending_key_change("!nobody")


def test_reject_pending_key_change_leaves_trusted_binding_untouched(tmp_path, clock):
    from nacl.signing import SigningKey

    from meshsrv.attachments.identity import load_signing_key
    from meshsrv.attachments.workspace import MCAWorkspaceManager as _WM

    _, _, _, coordinator_a = _make_node(tmp_path, "a", clock)
    _, _, principal_b, _ = _make_node(tmp_path, "b", clock)
    signing_key_b = load_signing_key(_WM(tmp_path / "b"), principal_b)
    first_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=principal_b.public_identity, epoch=0), signing_key_b
    )
    coordinator_a.handle_incoming(_direct_envelope(first_announce, "!node-b", clock()))
    coordinator_a.confirm_tofu("!node-b")
    confirmed_at = coordinator_a.get_binding("!node-b").tofu_confirmed_at

    impostor_key = SigningKey.generate()
    impostor_announce = codec.encode_key_announce(
        codec.KeyAnnounceFields(public_identity=bytes(impostor_key.verify_key), epoch=0), impostor_key
    )
    coordinator_a.handle_incoming(_direct_envelope(impostor_announce, "!node-b", clock()))
    assert coordinator_a.get_status("!node-b") == AddressStatus.KEY_CHANGED

    coordinator_a.reject_pending_key_change("!node-b")

    binding = coordinator_a.get_binding("!node-b")
    assert binding.public_identity == principal_b.public_identity  # untouched
    assert binding.tofu_confirmed_at == confirmed_at  # untouched
    assert binding.pending_public_identity is None
    assert binding.pending_key_epoch is None
    assert coordinator_a.get_status("!node-b") == AddressStatus.MCA_READY

    # Not a blacklist: the same impostor identity can be parked again later.
    coordinator_a.handle_incoming(_direct_envelope(impostor_announce, "!node-b", clock()))
    assert coordinator_a.get_status("!node-b") == AddressStatus.KEY_CHANGED


def test_reject_pending_key_change_without_a_pending_change_raises(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    with pytest.raises(KeyExchangeError):
        coordinator.reject_pending_key_change("!nobody")


# ---- outgoing KEY_REQUEST throttle (Step 1.6A.3C) -------------------------


def test_key_request_rate_limit_blocks_a_second_request_within_ten_minutes(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.check_key_request_rate_limit("!contact", clock())  # never sent -> allowed
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(60)
    with pytest.raises(RateLimited):
        coordinator.check_key_request_rate_limit("!contact", clock())


def test_key_request_rate_limit_allows_after_ten_minutes(tmp_path, clock):
    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.check_key_request_rate_limit("!contact", clock())
    coordinator.record_key_request_sent("!contact", clock())
    clock.advance(10 * 60 + 1)
    coordinator.check_key_request_rate_limit("!contact", clock())  # no raise


def test_key_request_rate_limit_survives_a_new_coordinator_instance(tmp_path, clock):
    """The outgoing key-request quota is persisted (`last_request_sent_at`,
    migration 13), so a process restart - modelled as a fresh coordinator over
    the same connection - still enforces the interval."""
    conn, workspace_manager, principal, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.check_key_request_rate_limit("!contact", clock())
    coordinator.record_key_request_sent("!contact", clock())

    restarted = KeyExchangeCoordinator(conn, workspace_manager, principal, "fake-text", now_fn=clock)
    clock.advance(60)
    with pytest.raises(RateLimited):
        restarted.check_key_request_rate_limit("!contact", clock())


def test_recording_a_key_request_does_not_block_an_announce(tmp_path, clock):
    """The two throttles are independent: recording an outgoing key request
    must not consume the KEY_ANNOUNCE gate (a subsequent inbound KEY_REQUEST
    is still answered)."""
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    coordinator.check_key_request_rate_limit("!contact", clock())
    coordinator.record_key_request_sent("!contact", clock())

    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())
    reply = coordinator.handle_incoming(_direct_envelope(request, "!contact", clock()))
    assert reply is not None


def test_an_announce_does_not_block_a_key_request(tmp_path, clock):
    """The two throttles are independent: recording an automatic KEY_ANNOUNCE
    must not consume the outgoing key-request gate."""
    from nacl.signing import SigningKey

    _, _, _, coordinator = _make_node(tmp_path, "a", clock)
    request = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=b"\x02" * 8), SigningKey.generate())
    coordinator.handle_incoming(_direct_envelope(request, "!contact", clock()))  # records announce

    coordinator.check_key_request_rate_limit("!contact", clock())  # no raise


# ---- full two-node round trip over the fake transport contract ---------


def test_full_round_trip_over_fake_text_adapter(tmp_path, clock):
    """End-to-end: node A requests node B's key over a real
    DeliveryAdapter (FakeTextAdapter/InMemoryEther, Step 0.4's contract),
    B answers with a self-signed KEY_ANNOUNCE, A processes it into an
    unverified binding and replies KEY_ACK, B accepts the ack - all
    through encode()/send()/ingest(), never touching internals directly."""
    ether = InMemoryEther()
    adapter_a = FakeTextAdapter(ether, "!node-a")
    adapter_b = FakeTextAdapter(ether, "!node-b")

    conn_a, wm_a, principal_a, coord_a = _make_node(tmp_path, "a", clock)
    conn_b, wm_b, principal_b, coord_b = _make_node(tmp_path, "b", clock)

    # A sends KEY_REQUEST to B.
    request_bytes = coord_a.build_key_request()
    route_to_b = adapter_a.resolve_route({"address": "!node-b"})
    wire = adapter_a.encode(request_bytes, route_to_b)
    adapter_a.send(wire, route_to_b, idempotency_key="req-1")

    # B receives it.
    [event] = ether.drain("!node-b")
    envelope = adapter_b.ingest(event)
    assert envelope is not None
    reply_bytes = coord_b.handle_incoming(envelope)
    assert reply_bytes is not None  # B's self-signed KEY_ANNOUNCE

    # B sends its announce back to A.
    route_to_a = adapter_b.resolve_route({"address": "!node-a"})
    wire_back = adapter_b.encode(reply_bytes, route_to_a)
    adapter_b.send(wire_back, route_to_a, idempotency_key="announce-1")

    # A receives it.
    [event_back] = ether.drain("!node-a")
    envelope_back = adapter_a.ingest(event_back)
    assert envelope_back is not None
    ack_bytes = coord_a.handle_incoming(envelope_back)
    assert ack_bytes is not None

    binding = coord_a.get_binding("!node-b")
    assert binding is not None
    assert binding.public_identity == principal_b.public_identity
    assert binding.tofu_confirmed_at is None

    coord_a.confirm_tofu("!node-b")
    assert coord_a.get_status("!node-b") == AddressStatus.MCA_READY
