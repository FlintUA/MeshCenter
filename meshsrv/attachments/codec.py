"""MCA/1 message codec — MIT Core code, no Meshtastic/MeshCore import.

Implements the wire format fixed by docs/architecture/ADR-0001-mca-protocol.md:
canonical CBOR logical messages, signed with Ed25519, carried as either
`MCA1-TEXT` (``MCA1:`` + Base64URL, for text-capable transports) or
`MCA1-CBOR` (raw canonical CBOR bytes, for binary transports such as a future
MeshCore adapter).

This module only knows about the *logical* message shape and its signature.
It does not know about transfer state, Relay descriptors, recipient
envelopes, or key management — those are separate concerns (Attachment
Store, MCA Crypto's descriptor signing, MCAWorkspaceManager). A
``DeliveryAdapter`` is expected to call ``to_text()``/``from_text()`` (or
``to_cbor()``/``from_cbor()`` for binary transports) and enforce its own
size ceiling on the result of ``encode_offer()`` before ever touching a
transport.

Every encode/decode function raises ``CodecError`` (never a bare
``KeyError``/``struct.error``/etc.) on malformed input, so callers can treat
"any transport that hands MCAttach bytes" as adversarial input uniformly.
"""

from __future__ import annotations

import base64
import enum
from dataclasses import dataclass
from typing import Optional

import cbor2
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

PROTOCOL_VERSION = 1

# Hard ceilings from ADR-0001 section 4. These are the numbers a
# DeliveryAdapter checks *after* encode() - never a soft target baked into
# the encoder itself, so a future field addition fails loudly in a test
# instead of silently shipping an oversized message.
OFFER_MAX_ASCII_BYTES = 180
KEY_ANNOUNCE_MAX_ASCII_BYTES = 180

# Coarse guard against adversarial input before we ever hand bytes to the
# CBOR decoder. MVP messages top out at 122 raw CBOR bytes (OFFER); this is
# deliberately generous relative to that so a legitimate future field
# addition doesn't need this constant touched, while still rejecting
# obviously-hostile multi-kilobyte "MCA1:" text before spending any CPU on
# CBOR parsing or Base64 decoding.
MAX_MESSAGE_RAW_BYTES = 2048

TEXT_PREFIX = "MCA1:"


class CodecError(ValueError):
    """Raised for any malformed/oversized/unsupported MCA message.

    Deliberately a single exception type: callers (delivery adapters,
    ingest hooks) are expected to catch this once and treat the message as
    "not a valid MCA message" - they should never need to distinguish
    "bad base64" from "bad CBOR" from "wrong field type" to behave safely.
    """


class MessageType(enum.IntEnum):
    """Numeric ``message_type`` codes - see ADR-0001 section 5.

    Codes are assigned once, in the ADR, and never reused or reordered.
    """

    OFFER = 1
    ACK_RECEIVED = 2
    ACK_DOWNLOADED = 3
    ACK_PROVIDER_UNKNOWN = 4
    CANCEL = 5
    KEY_REQUEST = 6
    KEY_ANNOUNCE = 7
    KEY_ACK = 8
    REJECTED = 9
    EXPIRED = 10
    KEY_ROTATE = 11


# Message types that share the minimal "simple ack" shape (ADR-0001 5.1):
# {0: version, 1: message_type, 2: transfer_id, 3: signature}.
_SIMPLE_ACK_TYPES = frozenset(
    {
        MessageType.ACK_RECEIVED,
        MessageType.ACK_DOWNLOADED,
        MessageType.ACK_PROVIDER_UNKNOWN,
        MessageType.CANCEL,
        MessageType.REJECTED,
        MessageType.EXPIRED,
    }
)

# Domain-separation labels for signing. Every message type signs a
# different label prefix, so a signature valid for one message type/shape
# can never be replayed as a signature over a different one, even if the
# underlying canonical bytes happened to collide (they can't, in practice,
# since CBOR encodes the message_type field - but domain separation is
# cheap insurance and the spec requires it explicitly, see ADR-0001 section
# 3 and the design spec's note that pointer signature and descriptor
# signature "must never" be treated as interchangeable).
_DOMAIN_LABELS = {
    MessageType.OFFER: b"MCA1/OFFER/v1",
    MessageType.KEY_ANNOUNCE: b"MCA1/KEY_ANNOUNCE/v1",
    MessageType.KEY_REQUEST: b"MCA1/KEY_REQUEST/v1",
    MessageType.KEY_ACK: b"MCA1/KEY_ACK/v1",
    MessageType.ACK_RECEIVED: b"MCA1/ACK_RECEIVED/v1",
    MessageType.ACK_DOWNLOADED: b"MCA1/ACK_DOWNLOADED/v1",
    MessageType.ACK_PROVIDER_UNKNOWN: b"MCA1/ACK_PROVIDER_UNKNOWN/v1",
    MessageType.CANCEL: b"MCA1/CANCEL/v1",
    MessageType.REJECTED: b"MCA1/REJECTED/v1",
    MessageType.EXPIRED: b"MCA1/EXPIRED/v1",
}


def _require_bytes(value: bytes, length: int, field_name: str) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != length:
        raise CodecError(
            f"{field_name} must be exactly {length} bytes, "
            f"got {type(value).__name__} of length "
            f"{len(value) if isinstance(value, (bytes, bytearray)) else 'n/a'}"
        )
    return bytes(value)


def _require_uint(value: int, field_name: str = "value", max_value: int = 2**32 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= max_value):
        raise CodecError(f"{field_name} must be an unsigned integer <= {max_value}, got {value!r}")
    return value


def _canonical_cbor(mapping: dict) -> bytes:
    return cbor2.dumps(mapping, canonical=True)


def _decode_cbor_map(raw: bytes) -> dict:
    if not isinstance(raw, (bytes, bytearray)):
        raise CodecError("MCA message bytes must be bytes")
    if len(raw) == 0:
        raise CodecError("MCA message is empty")
    if len(raw) > MAX_MESSAGE_RAW_BYTES:
        raise CodecError(
            f"MCA message of {len(raw)} bytes exceeds the {MAX_MESSAGE_RAW_BYTES}-byte "
            "hard ceiling - refusing to parse"
        )
    try:
        obj = cbor2.loads(bytes(raw))
    except RecursionError as exc:
        # A pathologically deeply-nested CBOR document can blow the
        # interpreter's recursion limit before hitting our byte-size cap.
        # Converting to CodecError keeps this a normal "reject the
        # message" outcome instead of an uncaught RecursionError bubbling
        # into whatever background worker is parsing incoming radio text.
        raise CodecError("MCA message CBOR nesting too deep") from exc
    except (cbor2.CBORDecodeError, ValueError, EOFError, TypeError) as exc:
        raise CodecError(f"malformed CBOR: {exc}") from exc
    if not isinstance(obj, dict):
        raise CodecError("MCA message must decode to a CBOR map")
    return obj


def _get_field(obj: dict, key: int, field_name: str):
    if key not in obj:
        raise CodecError(f"missing required field {field_name!r} (key {key})")
    return obj[key]


def _check_version_and_type(obj: dict, expected_type: MessageType) -> None:
    version = _get_field(obj, 0, "version")
    if version != PROTOCOL_VERSION:
        raise CodecError(f"unsupported protocol version {version!r} (expected {PROTOCOL_VERSION})")
    message_type = _get_field(obj, 1, "message_type")
    try:
        actual_type = MessageType(message_type)
    except ValueError as exc:
        raise CodecError(f"unknown message_type {message_type!r}") from exc
    if actual_type is not expected_type:
        raise CodecError(f"expected message_type {expected_type.name}, got {actual_type.name}")


def _sign(signing_key: SigningKey, message_type: MessageType, unsigned_canonical: bytes) -> bytes:
    label = _DOMAIN_LABELS[message_type]
    return signing_key.sign(label + unsigned_canonical).signature


def _verify(verify_key: VerifyKey, message_type: MessageType, unsigned_canonical: bytes, signature: bytes) -> None:
    label = _DOMAIN_LABELS[message_type]
    try:
        verify_key.verify(label + unsigned_canonical, signature)
    except BadSignatureError as exc:
        raise CodecError("signature verification failed") from exc


# --------------------------------------------------------------------------
# OFFER
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class OfferFields:
    provider_id: bytes  # 8 bytes
    transfer_id: bytes  # 16 bytes
    sender_key_id: bytes  # 8 bytes
    kind: int
    size_bucket: int
    hard_expires_at: int  # unix time UTC, uint32
    flags: int
    signature: Optional[bytes] = None  # 64 bytes, set by encode_offer()


def encode_offer(fields: OfferFields, signing_key: SigningKey) -> bytes:
    """Encode an OFFER to canonical CBOR bytes, signing it with ``signing_key``.

    Returns the full canonical CBOR (122 bytes for a maximally-populated
    OFFER per ADR-0001 section 4). Callers that need ``MCA1-TEXT`` should
    pass the result through :func:`to_text`.
    """
    provider_id = _require_bytes(fields.provider_id, 8, "provider_id")
    transfer_id = _require_bytes(fields.transfer_id, 16, "transfer_id")
    sender_key_id = _require_bytes(fields.sender_key_id, 8, "sender_key_id")
    kind = _require_uint(fields.kind, "kind", max_value=255)
    size_bucket = _require_uint(fields.size_bucket, "size_bucket", max_value=255)
    hard_expires_at = _require_uint(fields.hard_expires_at, "hard_expires_at", max_value=2**32 - 1)
    flags = _require_uint(fields.flags, "flags", max_value=255)

    unsigned = {
        0: PROTOCOL_VERSION,
        1: int(MessageType.OFFER),
        2: provider_id,
        3: transfer_id,
        4: sender_key_id,
        5: kind,
        6: size_bucket,
        7: hard_expires_at,
        8: flags,
    }
    signature = _sign(signing_key, MessageType.OFFER, _canonical_cbor(unsigned))
    unsigned[9] = signature
    return _canonical_cbor(unsigned)


def decode_offer(raw: bytes, verify_key: Optional[VerifyKey] = None) -> OfferFields:
    """Decode canonical CBOR bytes into :class:`OfferFields`.

    If ``verify_key`` is given, the signature is checked and
    :class:`CodecError` is raised on failure. If omitted, the caller is
    expected to look up the right key by ``sender_key_id`` and verify
    separately (useful for a first-contact OFFER where the key isn't known
    yet and the message must still be *parsed* to reach ``sender_key_id``).
    """
    obj = _decode_cbor_map(raw)
    _check_version_and_type(obj, MessageType.OFFER)

    provider_id = _require_bytes(_get_field(obj, 2, "provider_id"), 8, "provider_id")
    transfer_id = _require_bytes(_get_field(obj, 3, "transfer_id"), 16, "transfer_id")
    sender_key_id = _require_bytes(_get_field(obj, 4, "sender_key_id"), 8, "sender_key_id")
    kind = _require_uint(_get_field(obj, 5, "kind"), "kind", max_value=255)
    size_bucket = _require_uint(_get_field(obj, 6, "size_bucket"), "size_bucket", max_value=255)
    hard_expires_at = _require_uint(_get_field(obj, 7, "hard_expires_at"), "hard_expires_at", max_value=2**32 - 1)
    flags = _require_uint(_get_field(obj, 8, "flags"), "flags", max_value=255)
    signature = _require_bytes(_get_field(obj, 9, "signature"), 64, "signature")

    if verify_key is not None:
        unsigned = {
            0: PROTOCOL_VERSION,
            1: int(MessageType.OFFER),
            2: provider_id,
            3: transfer_id,
            4: sender_key_id,
            5: kind,
            6: size_bucket,
            7: hard_expires_at,
            8: flags,
        }
        _verify(verify_key, MessageType.OFFER, _canonical_cbor(unsigned), signature)

    return OfferFields(
        provider_id=provider_id,
        transfer_id=transfer_id,
        sender_key_id=sender_key_id,
        kind=kind,
        size_bucket=size_bucket,
        hard_expires_at=hard_expires_at,
        flags=flags,
        signature=signature,
    )


# --------------------------------------------------------------------------
# KEY_ANNOUNCE
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyAnnounceFields:
    public_identity: bytes  # 32 bytes, Ed25519 public key
    epoch: int
    signature: Optional[bytes] = None


def encode_key_announce(fields: KeyAnnounceFields, signing_key: SigningKey) -> bytes:
    """Encode a self-signed KEY_ANNOUNCE.

    ``signing_key`` must correspond to ``fields.public_identity`` - this is
    a self-signature, not signed by some other identity.
    """
    public_identity = _require_bytes(fields.public_identity, 32, "public_identity")
    epoch = _require_uint(fields.epoch, "epoch")

    unsigned = {
        0: PROTOCOL_VERSION,
        1: int(MessageType.KEY_ANNOUNCE),
        2: public_identity,
        3: epoch,
    }
    signature = _sign(signing_key, MessageType.KEY_ANNOUNCE, _canonical_cbor(unsigned))
    unsigned[4] = signature
    return _canonical_cbor(unsigned)


def decode_key_announce(raw: bytes, verify_against_self: bool = True) -> KeyAnnounceFields:
    """Decode a KEY_ANNOUNCE.

    KEY_ANNOUNCE is self-signed: the verify key for the signature *is* the
    announced ``public_identity`` field. ``verify_against_self=True`` (the
    default) checks that self-signature - a KEY_ANNOUNCE that doesn't
    validate against its own announced key is definitely malformed/hostile
    regardless of any other trust decision (TOFU, key-change warnings) the
    caller still needs to make afterwards.
    """
    obj = _decode_cbor_map(raw)
    _check_version_and_type(obj, MessageType.KEY_ANNOUNCE)

    public_identity = _require_bytes(_get_field(obj, 2, "public_identity"), 32, "public_identity")
    epoch = _require_uint(_get_field(obj, 3, "epoch"), "epoch")
    signature = _require_bytes(_get_field(obj, 4, "signature"), 64, "signature")

    if verify_against_self:
        unsigned = {
            0: PROTOCOL_VERSION,
            1: int(MessageType.KEY_ANNOUNCE),
            2: public_identity,
            3: epoch,
        }
        verify_key = VerifyKey(public_identity)
        _verify(verify_key, MessageType.KEY_ANNOUNCE, _canonical_cbor(unsigned), signature)

    return KeyAnnounceFields(public_identity=public_identity, epoch=epoch, signature=signature)


# --------------------------------------------------------------------------
# KEY_REQUEST / KEY_ACK
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyRequestFields:
    sender_key_id: bytes  # 8 bytes
    signature: Optional[bytes] = None


def encode_key_request(fields: KeyRequestFields, signing_key: SigningKey) -> bytes:
    sender_key_id = _require_bytes(fields.sender_key_id, 8, "sender_key_id")
    unsigned = {0: PROTOCOL_VERSION, 1: int(MessageType.KEY_REQUEST), 2: sender_key_id}
    signature = _sign(signing_key, MessageType.KEY_REQUEST, _canonical_cbor(unsigned))
    unsigned[3] = signature
    return _canonical_cbor(unsigned)


def decode_key_request(raw: bytes, verify_key: Optional[VerifyKey] = None) -> KeyRequestFields:
    obj = _decode_cbor_map(raw)
    _check_version_and_type(obj, MessageType.KEY_REQUEST)
    sender_key_id = _require_bytes(_get_field(obj, 2, "sender_key_id"), 8, "sender_key_id")
    signature = _require_bytes(_get_field(obj, 3, "signature"), 64, "signature")
    if verify_key is not None:
        unsigned = {0: PROTOCOL_VERSION, 1: int(MessageType.KEY_REQUEST), 2: sender_key_id}
        _verify(verify_key, MessageType.KEY_REQUEST, _canonical_cbor(unsigned), signature)
    return KeyRequestFields(sender_key_id=sender_key_id, signature=signature)


@dataclass(frozen=True)
class KeyAckFields:
    sender_key_id: bytes  # 8 bytes
    epoch: int
    signature: Optional[bytes] = None


def encode_key_ack(fields: KeyAckFields, signing_key: SigningKey) -> bytes:
    sender_key_id = _require_bytes(fields.sender_key_id, 8, "sender_key_id")
    epoch = _require_uint(fields.epoch, "epoch")
    unsigned = {0: PROTOCOL_VERSION, 1: int(MessageType.KEY_ACK), 2: sender_key_id, 3: epoch}
    signature = _sign(signing_key, MessageType.KEY_ACK, _canonical_cbor(unsigned))
    unsigned[4] = signature
    return _canonical_cbor(unsigned)


def decode_key_ack(raw: bytes, verify_key: Optional[VerifyKey] = None) -> KeyAckFields:
    obj = _decode_cbor_map(raw)
    _check_version_and_type(obj, MessageType.KEY_ACK)
    sender_key_id = _require_bytes(_get_field(obj, 2, "sender_key_id"), 8, "sender_key_id")
    epoch = _require_uint(_get_field(obj, 3, "epoch"), "epoch")
    signature = _require_bytes(_get_field(obj, 4, "signature"), 64, "signature")
    if verify_key is not None:
        unsigned = {0: PROTOCOL_VERSION, 1: int(MessageType.KEY_ACK), 2: sender_key_id, 3: epoch}
        _verify(verify_key, MessageType.KEY_ACK, _canonical_cbor(unsigned), signature)
    return KeyAckFields(sender_key_id=sender_key_id, epoch=epoch, signature=signature)


# --------------------------------------------------------------------------
# Simple acks: ACK_RECEIVED / ACK_DOWNLOADED / ACK_PROVIDER_UNKNOWN / CANCEL
# / REJECTED / EXPIRED - all {version, message_type, transfer_id, signature}
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SimpleAckFields:
    message_type: MessageType
    transfer_id: bytes  # 16 bytes
    signature: Optional[bytes] = None


def encode_simple_ack(message_type: MessageType, transfer_id: bytes, signing_key: SigningKey) -> bytes:
    if message_type not in _SIMPLE_ACK_TYPES:
        raise CodecError(f"{message_type.name} is not a simple-ack message type")
    transfer_id = _require_bytes(transfer_id, 16, "transfer_id")
    unsigned = {0: PROTOCOL_VERSION, 1: int(message_type), 2: transfer_id}
    signature = _sign(signing_key, message_type, _canonical_cbor(unsigned))
    unsigned[3] = signature
    return _canonical_cbor(unsigned)


def decode_simple_ack(
    raw: bytes,
    expected_type: MessageType,
    verify_key: Optional[VerifyKey] = None,
) -> SimpleAckFields:
    if expected_type not in _SIMPLE_ACK_TYPES:
        raise CodecError(f"{expected_type.name} is not a simple-ack message type")
    obj = _decode_cbor_map(raw)
    _check_version_and_type(obj, expected_type)
    transfer_id = _require_bytes(_get_field(obj, 2, "transfer_id"), 16, "transfer_id")
    signature = _require_bytes(_get_field(obj, 3, "signature"), 64, "signature")
    if verify_key is not None:
        unsigned = {0: PROTOCOL_VERSION, 1: int(expected_type), 2: transfer_id}
        _verify(verify_key, expected_type, _canonical_cbor(unsigned), signature)
    return SimpleAckFields(message_type=expected_type, transfer_id=transfer_id, signature=signature)


# --------------------------------------------------------------------------
# Transport encodings: MCA1-TEXT / MCA1-CBOR
# --------------------------------------------------------------------------


def to_text(canonical_cbor: bytes) -> str:
    """``MCA1-TEXT`` encoding: ``MCA1:`` + unpadded Base64URL of the CBOR bytes."""
    if not isinstance(canonical_cbor, (bytes, bytearray)) or len(canonical_cbor) == 0:
        raise CodecError("cannot encode empty/non-bytes payload as MCA1-TEXT")
    encoded = base64.urlsafe_b64encode(bytes(canonical_cbor)).rstrip(b"=").decode("ascii")
    return TEXT_PREFIX + encoded


def from_text(text: str) -> bytes:
    """Decode ``MCA1-TEXT`` back to canonical CBOR bytes.

    Raises :class:`CodecError` on anything that isn't a well-formed
    ``MCA1:``-prefixed, validly-padded Base64URL string - this is the
    first line of defense against non-MCA text messages and against
    adversarial Base64.
    """
    if not isinstance(text, str):
        raise CodecError("MCA1-TEXT input must be a string")
    if not text.startswith(TEXT_PREFIX):
        raise CodecError(f"not an MCA1-TEXT message (missing {TEXT_PREFIX!r} prefix)")
    if len(text) > MAX_MESSAGE_RAW_BYTES:
        raise CodecError(f"MCA1-TEXT message of {len(text)} chars exceeds the hard ceiling")
    body = text[len(TEXT_PREFIX):]
    # Re-pad for base64 decoding; urlsafe_b64encode strips padding on encode.
    padding = "=" * (-len(body) % 4)
    try:
        raw = base64.urlsafe_b64decode(body + padding)
    except (ValueError, TypeError) as exc:
        raise CodecError(f"malformed Base64URL in MCA1-TEXT: {exc}") from exc
    if len(raw) == 0:
        raise CodecError("MCA1-TEXT decoded to empty payload")
    return raw


def to_cbor(canonical_cbor: bytes) -> bytes:
    """``MCA1-CBOR`` encoding: the raw canonical CBOR bytes, unchanged.

    This function exists mainly for symmetry with :func:`to_text` and to
    give binary DeliveryAdapters (e.g. a future MeshCore adapter) one
    obvious call site to enforce their own size ceiling against, rather
    than reaching into codec internals.
    """
    if not isinstance(canonical_cbor, (bytes, bytearray)) or len(canonical_cbor) == 0:
        raise CodecError("cannot encode empty/non-bytes payload as MCA1-CBOR")
    return bytes(canonical_cbor)


def from_cbor(raw: bytes) -> bytes:
    """Decode ``MCA1-CBOR``: identity, with the same size guard as ``from_text``."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) == 0:
        raise CodecError("MCA1-CBOR input must be non-empty bytes")
    if len(raw) > MAX_MESSAGE_RAW_BYTES:
        raise CodecError(f"MCA1-CBOR message of {len(raw)} bytes exceeds the hard ceiling")
    return bytes(raw)


def peek_message_type(raw: bytes) -> MessageType:
    """Decode just enough to learn ``message_type`` without full field validation.

    Useful for an ingest hook that needs to route to the right
    ``decode_*`` function (and, for OFFER, look up ``sender_key_id`` before
    it has a verify key) without duplicating CBOR parsing. Still goes
    through the same size/format guards as every other decode path.
    """
    obj = _decode_cbor_map(raw)
    version = _get_field(obj, 0, "version")
    if version != PROTOCOL_VERSION:
        raise CodecError(f"unsupported protocol version {version!r} (expected {PROTOCOL_VERSION})")
    message_type = _get_field(obj, 1, "message_type")
    try:
        return MessageType(message_type)
    except ValueError as exc:
        raise CodecError(f"unknown message_type {message_type!r}") from exc
