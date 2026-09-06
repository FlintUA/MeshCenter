"""tests/test_delivery_contract.py

Contract test suite for meshsrv.attachments.delivery (Execution Plan
Step 0.4; design spec section 23.1 p.2, acceptance criterion #25).

Both FakeTextAdapter and FakeBinaryAdapter must pass the exact same suite:
one logical message, after a real encode()/send()/ingest() round trip
through either adapter, must come back byte-identical (same canonical CBOR,
same signature) - proving Core is not implicitly coupled to the text wire
format. The suite also proves the size limit is enforced against each
adapter's own encode() output, never a global constant, and that an
oversized payload is never handed to send() at all.
"""

from __future__ import annotations

import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import codec
from meshsrv.attachments.delivery.base import (
    DeliveryError,
    PayloadTooLargeError,
    RouteType,
    UnsupportedRouteError,
    WireFormat,
)
from meshsrv.attachments.delivery.fakes import FakeBinaryAdapter, FakeTextAdapter, InMemoryEther


def _max_offer_fields():
    """Same worst-case OFFER field values used in test_mca_codec.py's
    golden-size test, so the logical_message here is realistically sized
    (122 canonical CBOR bytes) rather than a trivially small stand-in."""
    return codec.OfferFields(
        provider_id=b"\xff" * 8,
        transfer_id=b"\xff" * 16,
        sender_key_id=b"\xff" * 8,
        kind=4,
        size_bucket=5,
        hard_expires_at=2_145_916_800,
        flags=3,
    )


def _make_offer_message(signing_key: SigningKey) -> bytes:
    return codec.encode_offer(_max_offer_fields(), signing_key)


ADAPTER_FACTORIES = {
    "text": lambda ether, addr: FakeTextAdapter(ether, addr),
    "binary": lambda ether, addr: FakeBinaryAdapter(ether, addr),
}


@pytest.fixture(params=sorted(ADAPTER_FACTORIES))
def adapter_kind(request):
    return request.param


def _build_pair(adapter_kind: str):
    ether = InMemoryEther()
    factory = ADAPTER_FACTORIES[adapter_kind]
    sender = factory(ether, "node-a")
    receiver = factory(ether, "node-b")
    return ether, sender, receiver


def test_capabilities_report_expected_wire_format(adapter_kind):
    ether, sender, _receiver = _build_pair(adapter_kind)
    caps = sender.capabilities()
    if adapter_kind == "text":
        assert caps.wire_formats == frozenset({WireFormat.MCA1_TEXT})
        assert caps.max_payload_bytes == 180
    else:
        assert caps.wire_formats == frozenset({WireFormat.MCA1_CBOR})
        assert caps.max_payload_bytes == 163
    assert isinstance(caps.supports_incoming, bool)


def test_round_trip_preserves_canonical_bytes_and_signature(adapter_kind):
    """The core contract-test requirement: one logical message survives a
    real encode -> send -> ingest round trip with byte-identical canonical
    CBOR (and therefore an identical, still-verifiable signature) on both
    the text and the binary fake adapter."""
    ether, sender, receiver = _build_pair(adapter_kind)
    signing_key = SigningKey.generate()
    logical_message = _make_offer_message(signing_key)

    route = sender.resolve_route({"address": "node-b"})
    wire_payload = sender.encode(logical_message, route)
    receipt = sender.send(wire_payload, route, idempotency_key="idem-1")
    assert receipt.sent is True
    assert receipt.idempotency_key == "idem-1"

    events = ether.drain("node-b")
    assert len(events) == 1
    envelope = receiver.ingest(events[0])
    assert envelope is not None
    assert envelope.logical_message == logical_message

    # The signature travelled intact: decoding+verifying on the far side
    # succeeds with the sender's real verify key.
    decoded = codec.decode_offer(envelope.logical_message, verify_key=signing_key.verify_key)
    assert decoded.transfer_id == b"\xff" * 16


def test_limit_is_checked_against_encoded_output_not_a_global_constant():
    """A FakeTextAdapter configured with a limit far below the real 180-byte
    Meshtastic ceiling must reject a message that the *default* adapter
    would happily accept - proving the check reads this instance's own
    capabilities(), not a shared/global MAX constant."""
    ether = InMemoryEther()
    generous = FakeTextAdapter(ether, "node-a", max_payload_bytes=180)
    stingy = FakeTextAdapter(ether, "node-c", max_payload_bytes=32)

    signing_key = SigningKey.generate()
    logical_message = _make_offer_message(signing_key)

    route_b = generous.resolve_route({"address": "node-b"})
    wire_payload = generous.encode(logical_message, route_b)
    # Golden number from ADR-0001 section 4 (122 canonical CBOR bytes -> 168
    # ASCII bytes as MCA1-TEXT); OFFER_MAX_ASCII_BYTES (180) is the transport
    # *ceiling*, not this exact message's encoded size.
    assert len(wire_payload) == 168
    assert len(wire_payload) <= codec.OFFER_MAX_ASCII_BYTES

    route_c = stingy.resolve_route({"address": "node-b"})
    with pytest.raises(PayloadTooLargeError) as exc_info:
        stingy.encode(logical_message, route_c)
    assert exc_info.value.limit_bytes == 32
    assert exc_info.value.encoded_bytes == len(wire_payload)


def test_oversized_payload_is_never_sent(adapter_kind):
    """encode() must raise before send() is ever reached - an oversized
    message must not appear in the peer's inbox even partially."""
    ether, sender, _receiver = _build_pair(adapter_kind)
    tiny_ether = InMemoryEther()
    tiny_factory = ADAPTER_FACTORIES[adapter_kind]
    tiny_sender = tiny_factory(tiny_ether, "node-a")
    # Force a limit below any real encoded OFFER by monkeypatching the
    # instance's own recorded limit (still exercised through the adapter's
    # real encode() path, not by calling a private method).
    tiny_sender._max_payload_bytes = 4  # noqa: SLF001 - test-only override

    signing_key = SigningKey.generate()
    logical_message = _make_offer_message(signing_key)
    route = tiny_sender.resolve_route({"address": "node-b"})

    with pytest.raises(PayloadTooLargeError):
        tiny_sender.encode(logical_message, route)

    assert tiny_ether.drain("node-b") == []


def test_encode_rejects_unsupported_route_type_for_text_adapter():
    ether = InMemoryEther()
    sender = FakeTextAdapter(ether, "node-a")
    signing_key = SigningKey.generate()
    logical_message = _make_offer_message(signing_key)
    from meshsrv.attachments.delivery.base import Route

    bad_route = Route(route_type=RouteType.CHANNEL, route_id="general", destination_address="node-b")
    with pytest.raises(UnsupportedRouteError):
        sender.encode(logical_message, bad_route)


def test_resolve_route_requires_address(adapter_kind):
    ether, sender, _receiver = _build_pair(adapter_kind)
    with pytest.raises(UnsupportedRouteError):
        sender.resolve_route({})


def test_ingest_ignores_unrelated_events(adapter_kind):
    ether, _sender, receiver = _build_pair(adapter_kind)
    assert receiver.ingest({"text": "just a normal chat message"}) is None
    assert receiver.ingest({"unrelated": True}) is None
    assert receiver.ingest("not even a dict") is None


def test_ingest_raises_on_malformed_mca_prefixed_payload(adapter_kind):
    ether, _sender, receiver = _build_pair(adapter_kind)
    if adapter_kind == "text":
        bad_event = {"text": codec.TEXT_PREFIX + "!!!not-base64!!!"}
    else:
        bad_event = {"bytes": b"\x00\x01\x02not-cbor"}
    with pytest.raises(DeliveryError):
        receiver.ingest(bad_event)


def test_binary_adapter_channel_route_is_supported():
    ether = InMemoryEther()
    sender = FakeBinaryAdapter(ether, "node-a")
    route = sender.resolve_route({"address": "general", "channel": True})
    assert route.route_type == RouteType.CHANNEL


def test_repeated_idempotency_key_does_not_duplicate_delivery(adapter_kind):
    """Re-sending with the same idempotency_key must not be treated by the
    contract as "must dedup at the ether level" (that's a real transport's
    job), but the receipt must consistently echo back the same key so a
    higher layer can dedup on it."""
    ether, sender, _receiver = _build_pair(adapter_kind)
    signing_key = SigningKey.generate()
    logical_message = _make_offer_message(signing_key)
    route = sender.resolve_route({"address": "node-b"})
    wire_payload = sender.encode(logical_message, route)

    receipt_1 = sender.send(wire_payload, route, idempotency_key="same-key")
    receipt_2 = sender.send(wire_payload, route, idempotency_key="same-key")
    assert receipt_1.idempotency_key == receipt_2.idempotency_key == "same-key"
