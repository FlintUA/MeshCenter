"""meshsrv/attachments/manifest.py

The MCA manifest blob (ADR-0006): the single opaque blob uploaded via
``relay_client.upload_manifest()`` and later downloaded and hashed against
``ObjectDescriptor.manifest_sha256``. Packs the "encrypted manifest" and
every per-recipient "sealed envelope" from design spec 8.2 into one
canonical-CBOR structure, so the Relay's own already-signed
``manifest_sha256`` transitively covers all of it - see ADR-0006 for the
full trust-chain reasoning. This module does no I/O and does not know
about the Relay or the attachments database; it only builds/parses bytes.

Depends on ``crypto.py`` (chunk/header AEAD) and ``identity.py`` (X25519
derivation for sealing/unsealing), never the reverse.
"""

from __future__ import annotations

import dataclasses
from typing import List, Optional, Sequence

import cbor2
from nacl.public import PrivateKey, PublicKey, SealedBox

from meshsrv.attachments import crypto

_VERSION = 1

# Plaintext header field keys (spec 8.2's "encrypted manifest").
_H_FILE_NAME = 0
_H_MIME_TYPE = 1
_H_PLAIN_SIZE = 2
_H_PLAIN_SHA256 = 3
_H_CHUNK_COUNT = 4
_H_COMMENT = 5

# Sealed envelope plaintext field keys (spec 8.2's per-recipient secret).
_E_DATA_KEY = 0
_E_NONCE_PREFIX = 1
_E_RECEIPT_SECRET = 2
_E_CHUNK_COUNT = 3

# Outer blob field keys.
_B_VERSION = 0
_B_TRANSFER_ID = 1
_B_ENCRYPTED_HEADER = 2
_B_RECIPIENTS = 3
_BH_NONCE = 0
_BH_CIPHERTEXT = 1
_BR_RECIPIENT_KEY_ID = 0
_BR_SEALED_ENVELOPE = 1


class ManifestError(ValueError):
    """Raised for any malformed manifest blob, sealed envelope, or header -
    a single exception type, matching ``codec.CodecError``'s rationale:
    callers treat any failure here as "this object cannot be trusted",
    never as a crash to propagate."""


@dataclasses.dataclass(frozen=True)
class ManifestHeader:
    """The plaintext fields of the "encrypted manifest" (spec 8.2)."""

    file_name: str
    mime_type: str
    plain_size: int
    plain_sha256: bytes  # raw 32 bytes
    chunk_count: int
    comment: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class RecipientSecret:
    """The plaintext sealed inside one recipient's envelope (spec 8.2)."""

    data_key: bytes  # raw 32 bytes
    nonce_prefix: bytes  # raw 16 bytes
    receipt_secret: bytes  # raw 32 bytes
    chunk_count: int


@dataclasses.dataclass(frozen=True)
class RecipientEnvelope:
    """One entry in the manifest blob's ordered recipient list."""

    recipient_key_id: bytes  # raw 8 bytes
    sealed_envelope: bytes  # SealedBox ciphertext


def _encode_header(header: ManifestHeader) -> bytes:
    if len(header.plain_sha256) != 32:
        raise ManifestError(f"plain_sha256 must be 32 raw bytes, got {len(header.plain_sha256)}")
    mapping = {
        _H_FILE_NAME: header.file_name,
        _H_MIME_TYPE: header.mime_type,
        _H_PLAIN_SIZE: header.plain_size,
        _H_PLAIN_SHA256: header.plain_sha256,
        _H_CHUNK_COUNT: header.chunk_count,
    }
    if header.comment is not None:
        mapping[_H_COMMENT] = header.comment
    return cbor2.dumps(mapping, canonical=True)


def _decode_header(raw: bytes) -> ManifestHeader:
    try:
        obj = cbor2.loads(bytes(raw))
    except Exception as exc:  # noqa: BLE001
        raise ManifestError("manifest header is not valid CBOR") from exc
    if not isinstance(obj, dict):
        raise ManifestError("manifest header must be a CBOR map")
    try:
        return ManifestHeader(
            file_name=str(obj[_H_FILE_NAME]),
            mime_type=str(obj[_H_MIME_TYPE]),
            plain_size=int(obj[_H_PLAIN_SIZE]),
            plain_sha256=bytes(obj[_H_PLAIN_SHA256]),
            chunk_count=int(obj[_H_CHUNK_COUNT]),
            comment=(str(obj[_H_COMMENT]) if _H_COMMENT in obj else None),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError("manifest header is missing/malformed a required field") from exc


def _encode_recipient_secret(secret: RecipientSecret) -> bytes:
    if len(secret.data_key) != crypto.DATA_KEY_BYTES:
        raise ManifestError(f"data_key must be {crypto.DATA_KEY_BYTES} raw bytes")
    if len(secret.nonce_prefix) != crypto.NONCE_PREFIX_BYTES:
        raise ManifestError(f"nonce_prefix must be {crypto.NONCE_PREFIX_BYTES} raw bytes")
    if len(secret.receipt_secret) != 32:
        raise ManifestError("receipt_secret must be 32 raw bytes")
    mapping = {
        _E_DATA_KEY: secret.data_key,
        _E_NONCE_PREFIX: secret.nonce_prefix,
        _E_RECEIPT_SECRET: secret.receipt_secret,
        _E_CHUNK_COUNT: secret.chunk_count,
    }
    return cbor2.dumps(mapping, canonical=True)


def _decode_recipient_secret(raw: bytes) -> RecipientSecret:
    try:
        obj = cbor2.loads(bytes(raw))
    except Exception as exc:  # noqa: BLE001
        raise ManifestError("sealed envelope plaintext is not valid CBOR") from exc
    if not isinstance(obj, dict):
        raise ManifestError("sealed envelope plaintext must be a CBOR map")
    try:
        return RecipientSecret(
            data_key=bytes(obj[_E_DATA_KEY]),
            nonce_prefix=bytes(obj[_E_NONCE_PREFIX]),
            receipt_secret=bytes(obj[_E_RECEIPT_SECRET]),
            chunk_count=int(obj[_E_CHUNK_COUNT]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError("sealed envelope plaintext is missing/malformed a required field") from exc


def seal_recipient_secret(secret: RecipientSecret, recipient_public_identity: bytes) -> bytes:
    """Seal ``secret`` for one recipient, identified by their 32-byte
    Ed25519 public identity. Uses ``identity.derive_x25519_public`` so the
    recipient can open it with the X25519 private key their own
    ``identity.derive_x25519_private`` produces - both directions of the
    same ADR-0002-confirmed derivation."""

    from meshsrv.attachments import identity  # local import: avoid a hard cycle at module load time

    x25519_public = identity.derive_x25519_public(recipient_public_identity)
    box = SealedBox(PublicKey(x25519_public))
    return box.encrypt(_encode_recipient_secret(secret))


def open_recipient_secret(sealed: bytes, recipient_signing_key) -> RecipientSecret:
    """Open a sealed envelope addressed to us. ``recipient_signing_key`` is
    our own ``nacl.signing.SigningKey`` (e.g. from
    ``identity.load_signing_key()``); its matching X25519 private scalar is
    derived fresh, never cached, mirroring ``load_signing_key()``'s own
    "keep private key material's lifetime short" discipline."""

    from meshsrv.attachments import identity  # local import: avoid a hard cycle at module load time

    x25519_private = identity.derive_x25519_private(recipient_signing_key)
    box = SealedBox(PrivateKey(x25519_private))
    try:
        plaintext = box.decrypt(bytes(sealed))
    except Exception as exc:  # noqa: BLE001 - any libsodium/box failure means "not for us / tampered"
        raise ManifestError("sealed envelope could not be opened with this principal's key") from exc
    return _decode_recipient_secret(plaintext)


def build_manifest_blob(
    *,
    transfer_id: bytes,
    data_key: bytes,
    nonce_prefix: bytes,
    header: ManifestHeader,
    recipients: Sequence[RecipientEnvelope],
) -> bytes:
    """Build the full manifest blob (ADR-0006 schema): the AEAD-encrypted
    header plus the ordered list of already-sealed recipient envelopes.
    ``recipients`` must already be sealed (via ``seal_recipient_secret``) -
    this function only assembles and encrypts the header, it does not seal
    anything itself, so callers control recipient ordering explicitly."""

    if len(transfer_id) != 16:
        raise ManifestError(f"transfer_id must be 16 raw bytes, got {len(transfer_id)}")
    if not recipients:
        raise ManifestError("a manifest blob must have at least one recipient")

    header_plaintext = _encode_header(header)
    header_ciphertext = crypto.encrypt_header(
        data_key=data_key,
        nonce_prefix=nonce_prefix,
        transfer_id=transfer_id,
        chunk_count=header.chunk_count,
        plain_size=header.plain_size,
        header_plaintext=header_plaintext,
    )
    nonce = crypto.header_nonce(nonce_prefix)

    mapping = {
        _B_VERSION: _VERSION,
        _B_TRANSFER_ID: transfer_id,
        _B_ENCRYPTED_HEADER: {_BH_NONCE: nonce, _BH_CIPHERTEXT: header_ciphertext},
        _B_RECIPIENTS: [
            {_BR_RECIPIENT_KEY_ID: r.recipient_key_id, _BR_SEALED_ENVELOPE: r.sealed_envelope} for r in recipients
        ],
    }
    return cbor2.dumps(mapping, canonical=True)


@dataclasses.dataclass(frozen=True)
class ParsedManifestBlob:
    version: int
    transfer_id: bytes
    header_nonce: bytes
    header_ciphertext: bytes
    recipients: List[RecipientEnvelope]


def parse_manifest_blob(raw: bytes) -> ParsedManifestBlob:
    """Parse (but do not decrypt) a manifest blob. Decrypting the header
    requires the DEK, which only comes from opening one of the sealed
    envelopes first (see ``open_recipient_secret``) - this function is the
    receiver's very first step, run against the bytes it hashed and
    compared to ``ObjectDescriptor.manifest_sha256`` before ever calling
    this (ADR-0006's trust chain)."""

    try:
        obj = cbor2.loads(bytes(raw))
    except Exception as exc:  # noqa: BLE001
        raise ManifestError("manifest blob is not valid CBOR") from exc
    if not isinstance(obj, dict):
        raise ManifestError("manifest blob must be a CBOR map")
    try:
        version = int(obj[_B_VERSION])
        transfer_id = bytes(obj[_B_TRANSFER_ID])
        encrypted_header = obj[_B_ENCRYPTED_HEADER]
        header_nonce_ = bytes(encrypted_header[_BH_NONCE])
        header_ciphertext = bytes(encrypted_header[_BH_CIPHERTEXT])
        recipients_raw = obj[_B_RECIPIENTS]
        if not isinstance(recipients_raw, list) or not recipients_raw:
            raise ManifestError("manifest blob must have a non-empty recipients list")
        recipients = [
            RecipientEnvelope(
                recipient_key_id=bytes(r[_BR_RECIPIENT_KEY_ID]),
                sealed_envelope=bytes(r[_BR_SEALED_ENVELOPE]),
            )
            for r in recipients_raw
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ManifestError("manifest blob is missing/malformed a required field") from exc

    if version != _VERSION:
        raise ManifestError(f"unsupported manifest blob version {version}")
    if len(transfer_id) != 16:
        raise ManifestError("manifest blob transfer_id must be 16 raw bytes")

    return ParsedManifestBlob(
        version=version,
        transfer_id=transfer_id,
        header_nonce=header_nonce_,
        header_ciphertext=header_ciphertext,
        recipients=recipients,
    )


def decrypt_manifest_header(
    parsed: ParsedManifestBlob,
    *,
    data_key: bytes,
    nonce_prefix: bytes,
    chunk_count: int,
    plain_size: int,
) -> ManifestHeader:
    """Decrypt the header once the receiver has the DEK (from its own
    opened sealed envelope). ``chunk_count``/``plain_size`` are the values
    from that same opened ``RecipientSecret`` (mirrored into the plaintext
    header, per ADR-0006), used here only to reconstruct the AAD - the
    decrypted header's own ``chunk_count``/``plain_size`` are then
    cross-checked against them by the caller (not by this function, which
    only proves the ciphertext is authentic under the AAD it was given)."""

    try:
        plaintext = crypto.decrypt_header(
            data_key=data_key,
            nonce_prefix=nonce_prefix,
            transfer_id=parsed.transfer_id,
            chunk_count=chunk_count,
            plain_size=plain_size,
            ciphertext=parsed.header_ciphertext,
        )
    except crypto.CryptoError as exc:
        raise ManifestError("manifest header failed AEAD verification") from exc
    return _decode_header(plaintext)


def find_recipient_envelope(parsed: ParsedManifestBlob, recipient_key_id: bytes) -> RecipientEnvelope:
    for entry in parsed.recipients:
        if entry.recipient_key_id == recipient_key_id:
            return entry
    raise ManifestError(f"no envelope in this manifest is addressed to key_id {recipient_key_id.hex()}")
