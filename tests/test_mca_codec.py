"""Tests for the MCA/1 message codec (meshsrv/attachments/codec.py).

Covers the codec-level requirements from the MCAttach execution plan, Step
0.3: round-trip of every MVP message type, the golden OFFER byte-size
numbers fixed in docs/architecture/ADR-0001-mca-protocol.md, and parser
robustness against malformed/oversized/adversarial input. This module has
no dependency on Relay, Meshtastic, or the database - every test here runs
against pure functions.
"""

import string

import cbor2
import pytest
from nacl.signing import SigningKey

from meshsrv.attachments import codec


def _new_signing_key():
    return SigningKey.generate()


# --------------------------------------------------------------------------
# OFFER: round-trip, golden size, signature verification
# --------------------------------------------------------------------------


def _max_offer_fields():
    # Values chosen deliberately to hit the ADR-0001 "worst case" byte
    # budget: kind/size_bucket/flags stay below 24 (1-byte CBOR uint each),
    # hard_expires_at is a realistic 2026+ unix timestamp that needs the
    # full 4-byte uint encoding (anything above 2**24 does).
    return codec.OfferFields(
        provider_id=bytes(range(8)),
        transfer_id=bytes(range(16)),
        sender_key_id=bytes(range(8, 16)),
        kind=4,
        size_bucket=5,
        hard_expires_at=2_145_916_800,  # 2038-01-01, well above 2**24
        flags=3,
    )


def test_offer_round_trip():
    signing_key = _new_signing_key()
    fields = _max_offer_fields()

    raw = codec.encode_offer(fields, signing_key)
    decoded = codec.decode_offer(raw, verify_key=signing_key.verify_key)

    assert decoded.provider_id == fields.provider_id
    assert decoded.transfer_id == fields.transfer_id
    assert decoded.sender_key_id == fields.sender_key_id
    assert decoded.kind == fields.kind
    assert decoded.size_bucket == fields.size_bucket
    assert decoded.hard_expires_at == fields.hard_expires_at
    assert decoded.flags == fields.flags
    assert decoded.signature is not None and len(decoded.signature) == 64


def test_offer_golden_size_matches_adr_0001():
    """The exact numbers in ADR-0001 section 4: 122 CBOR bytes, 168 ASCII bytes."""
    signing_key = _new_signing_key()
    raw = codec.encode_offer(_max_offer_fields(), signing_key)

    assert len(raw) == 122, f"canonical CBOR OFFER should be 122 bytes, got {len(raw)}"

    text = codec.to_text(raw)
    assert len(text) == 168, f"MCA1-TEXT OFFER should be 168 ASCII bytes, got {len(text)}"
    assert len(text) <= codec.OFFER_MAX_ASCII_BYTES

    cbor_form = codec.to_cbor(raw)
    assert len(cbor_form) == 122, "MCA1-CBOR OFFER should be the same 122 bytes as canonical CBOR"


def test_offer_wrong_verify_key_is_rejected():
    fields = _max_offer_fields()
    raw = codec.encode_offer(fields, _new_signing_key())
    wrong_key = _new_signing_key()

    with pytest.raises(codec.CodecError):
        codec.decode_offer(raw, verify_key=wrong_key.verify_key)


def test_offer_can_be_parsed_without_verify_key_for_first_contact():
    # A first-contact OFFER from an unknown sender still needs to be
    # parseable (to reach sender_key_id and decide WAITING_KEY) before any
    # verify key is available - this is deliberate, not a bypass.
    fields = _max_offer_fields()
    raw = codec.encode_offer(fields, _new_signing_key())

    decoded = codec.decode_offer(raw)
    assert decoded.sender_key_id == fields.sender_key_id


def test_offer_tampered_field_is_detected():
    fields = _max_offer_fields()
    signing_key = _new_signing_key()
    raw = codec.encode_offer(fields, signing_key)

    obj = cbor2.loads(raw)
    obj[7] = obj[7] + 1  # tamper with hard_expires_at after signing
    tampered = cbor2.dumps(obj, canonical=True)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(tampered, verify_key=signing_key.verify_key)


# --------------------------------------------------------------------------
# KEY_ANNOUNCE: round-trip, self-signature, size ceiling
# --------------------------------------------------------------------------


def test_key_announce_round_trip_and_self_signature():
    signing_key = _new_signing_key()
    fields = codec.KeyAnnounceFields(
        public_identity=bytes(signing_key.verify_key),
        epoch=7,
    )

    raw = codec.encode_key_announce(fields, signing_key)
    decoded = codec.decode_key_announce(raw)

    assert decoded.public_identity == bytes(signing_key.verify_key)
    assert decoded.epoch == 7

    text = codec.to_text(raw)
    assert len(text) <= codec.KEY_ANNOUNCE_MAX_ASCII_BYTES, (
        f"KEY_ANNOUNCE MCA1-TEXT is {len(text)} bytes, "
        f"expected <= {codec.KEY_ANNOUNCE_MAX_ASCII_BYTES} per ADR-0001 section 4"
    )


def test_key_announce_rejects_mismatched_self_signature():
    signing_key = _new_signing_key()
    other_key = _new_signing_key()
    fields = codec.KeyAnnounceFields(
        public_identity=bytes(other_key.verify_key),  # doesn't match signing_key
        epoch=1,
    )
    raw = codec.encode_key_announce(fields, signing_key)

    with pytest.raises(codec.CodecError):
        codec.decode_key_announce(raw, verify_against_self=True)


# --------------------------------------------------------------------------
# KEY_REQUEST / KEY_ACK
# --------------------------------------------------------------------------


def test_key_request_round_trip():
    signing_key = _new_signing_key()
    fields = codec.KeyRequestFields(sender_key_id=bytes(range(8)))
    raw = codec.encode_key_request(fields, signing_key)

    decoded = codec.decode_key_request(raw, verify_key=signing_key.verify_key)
    assert decoded.sender_key_id == bytes(range(8))


def test_key_ack_round_trip():
    signing_key = _new_signing_key()
    fields = codec.KeyAckFields(sender_key_id=bytes(range(8)), epoch=2)
    raw = codec.encode_key_ack(fields, signing_key)

    decoded = codec.decode_key_ack(raw, verify_key=signing_key.verify_key)
    assert decoded.sender_key_id == bytes(range(8))
    assert decoded.epoch == 2


# --------------------------------------------------------------------------
# Simple acks: ACK_RECEIVED / ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN / CANCEL
# / REJECTED / EXPIRED
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message_type",
    [
        codec.MessageType.ACK_RECEIVED,
        codec.MessageType.ACK_DOWNLOADED,
        codec.MessageType.ACK_PROVIDER_UNKNOWN,
        codec.MessageType.CANCEL,
        codec.MessageType.REJECTED,
        codec.MessageType.EXPIRED,
    ],
)
def test_simple_ack_round_trip_every_type(message_type):
    signing_key = _new_signing_key()
    transfer_id = bytes(range(16))

    raw = codec.encode_simple_ack(message_type, transfer_id, signing_key)
    decoded = codec.decode_simple_ack(raw, message_type, verify_key=signing_key.verify_key)

    assert decoded.message_type == message_type
    assert decoded.transfer_id == transfer_id


def test_simple_ack_wrong_expected_type_is_rejected():
    signing_key = _new_signing_key()
    raw = codec.encode_simple_ack(codec.MessageType.CANCEL, bytes(range(16)), signing_key)

    with pytest.raises(codec.CodecError):
        codec.decode_simple_ack(raw, codec.MessageType.ACK_RECEIVED, verify_key=signing_key.verify_key)


def test_simple_ack_rejects_non_ack_type():
    with pytest.raises(codec.CodecError):
        codec.encode_simple_ack(codec.MessageType.OFFER, bytes(16), _new_signing_key())


# --------------------------------------------------------------------------
# MCA1-TEXT / MCA1-CBOR transport encoding
# --------------------------------------------------------------------------


def test_to_text_from_text_round_trip():
    raw = codec.encode_offer(_max_offer_fields(), _new_signing_key())
    text = codec.to_text(raw)

    assert text.startswith("MCA1:")
    assert set(text[len("MCA1:"):]) <= set(string.ascii_letters + string.digits + "-_")

    recovered = codec.from_text(text)
    assert recovered == raw


def test_from_text_rejects_missing_prefix():
    with pytest.raises(codec.CodecError):
        codec.from_text("not-an-mca-message")


def test_from_text_rejects_malformed_base64():
    with pytest.raises(codec.CodecError):
        codec.from_text("MCA1:not!!valid==base64url###")


def test_to_cbor_from_cbor_round_trip():
    raw = codec.encode_offer(_max_offer_fields(), _new_signing_key())
    wire = codec.to_cbor(raw)
    assert wire == raw
    assert codec.from_cbor(wire) == raw


# --------------------------------------------------------------------------
# Parser robustness: malformed/oversized/adversarial input
# --------------------------------------------------------------------------


def test_decode_offer_rejects_empty_bytes():
    with pytest.raises(codec.CodecError):
        codec.decode_offer(b"")


def test_decode_offer_rejects_non_cbor_garbage():
    with pytest.raises(codec.CodecError):
        codec.decode_offer(b"\xff\xff\xff\xff not cbor at all")


def test_decode_offer_rejects_oversized_message():
    huge = b"\x00" * (codec.MAX_MESSAGE_RAW_BYTES + 1)
    with pytest.raises(codec.CodecError):
        codec.decode_offer(huge)


def test_decode_offer_rejects_wrong_version():
    signing_key = _new_signing_key()
    raw = codec.encode_offer(_max_offer_fields(), signing_key)
    obj = cbor2.loads(raw)
    obj[0] = 99  # unsupported version
    tampered = cbor2.dumps(obj, canonical=True)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(tampered)


def test_decode_offer_rejects_unknown_message_type():
    obj = {0: codec.PROTOCOL_VERSION, 1: 250}  # 250 is not a defined MessageType
    tampered = cbor2.dumps(obj, canonical=True)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(tampered)


def test_decode_offer_rejects_wrong_message_type():
    # A well-formed KEY_REQUEST fed to decode_offer must be rejected, not
    # silently coerced.
    signing_key = _new_signing_key()
    raw = codec.encode_key_request(codec.KeyRequestFields(sender_key_id=bytes(8)), signing_key)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(raw)


def test_decode_offer_rejects_missing_field():
    obj = {0: codec.PROTOCOL_VERSION, 1: int(codec.MessageType.OFFER)}  # everything else missing
    tampered = cbor2.dumps(obj, canonical=True)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(tampered)


def test_decode_offer_rejects_wrong_field_length():
    signing_key = _new_signing_key()
    raw = codec.encode_offer(_max_offer_fields(), signing_key)
    obj = cbor2.loads(raw)
    obj[3] = b"\x00" * 15  # transfer_id must be exactly 16 bytes
    tampered = cbor2.dumps(obj, canonical=True)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(tampered)


def test_decode_offer_rejects_deeply_nested_cbor():
    # Build a pathologically nested CBOR array as a stand-in for a hostile
    # payload designed to exhaust the parser via recursion rather than
    # size. This must come back as CodecError, never an uncaught
    # RecursionError, regardless of overall byte size.
    nested = []
    cursor = nested
    for _ in range(5000):
        inner = []
        cursor.append(inner)
        cursor = inner
    raw = cbor2.dumps(nested)

    with pytest.raises(codec.CodecError):
        codec.decode_offer(raw)


def test_encode_offer_rejects_invalid_field_sizes():
    fields = _max_offer_fields()
    with pytest.raises(codec.CodecError):
        codec.encode_offer(
            codec.OfferFields(**{**fields.__dict__, "provider_id": b"\x00" * 7}),
            _new_signing_key(),
        )


@pytest.mark.parametrize("iteration", range(200))
def test_fuzz_decode_offer_never_raises_uncaught(iteration):
    """Feed pseudo-random bytes at every size up to the hard ceiling.

    The only acceptable outcomes are a successful decode (vanishingly
    unlikely for random bytes) or CodecError - anything else (a bare
    KeyError, struct.error, RecursionError escaping, etc.) is a parser bug.
    """
    import random

    rng = random.Random(iteration)
    length = rng.randint(0, 256)
    garbage = bytes(rng.getrandbits(8) for _ in range(length))

    try:
        codec.decode_offer(garbage)
    except codec.CodecError:
        pass  # expected outcome for almost all random input
