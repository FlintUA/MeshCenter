"""meshsrv/attachments/crypto.py

Chunk and header AEAD encryption for MCA objects (design spec section 8.2;
ADR-0006). This module knows nothing about the Relay, the manifest blob
format, or recipient key material - it only turns
``(data_key, nonce_prefix, transfer_id, chunk_count, plain_size)`` plus a
byte string into ciphertext and back. That narrow scope is deliberate: it
is unit-testable with zero I/O and zero Relay/manifest knowledge, and it is
the one place XChaCha20-Poly1305 nonce construction happens, so a nonce
bug is a one-file, one-function search.

Primitive: XChaCha20-Poly1305 (IETF construction, 24-byte nonce), via
PyNaCl's low-level binding - the same vetted-libsodium-binding requirement
ADR-0001 section 1 states for every MCA primitive.

Nonce construction (ADR-0006):
    chunk nonce  = nonce_prefix (16 bytes) || big-endian uint64(index)
    header nonce = nonce_prefix (16 bytes) || 0xFFFFFFFFFFFFFFFF

``index`` ranges over ``[0, chunk_count)``; chunk_count is bounded far
below 2**64 by the Relay's own ``max_chunks`` limit, so the header nonce's
all-ones suffix can never collide with a real chunk index.

AAD binds every ciphertext to its transfer, its role (header vs. a
specific chunk index), the total chunk count, and the overall plaintext
size - so truncation, reordering, or splicing ciphertext from a different
transfer/version fails AEAD verification, not just a later hash
comparison.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import cbor2
import nacl.bindings as sodium

DATA_KEY_BYTES = sodium.crypto_aead_xchacha20poly1305_ietf_KEYBYTES  # 32
NONCE_PREFIX_BYTES = 16
NONCE_BYTES = sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES  # 24
_NONCE_SUFFIX_BYTES = NONCE_BYTES - NONCE_PREFIX_BYTES  # 8
_HEADER_NONCE_SUFFIX = b"\xff" * _NONCE_SUFFIX_BYTES

CHUNK_SIZE_BYTES = 256 * 1024

_AAD_VERSION = 1
_ROLE_CHUNK = "chunk"
_ROLE_HEADER = "header"


class CryptoError(ValueError):
    """Raised for any malformed key/nonce material or AEAD verification failure.

    A single exception type, matching ``codec.CodecError``'s own rationale:
    callers should not need to distinguish "wrong key length" from "tag
    mismatch" from "truncated ciphertext" to react safely - all of them mean
    "do not trust this plaintext".
    """


def _require_length(name: str, value: bytes, expected: int) -> None:
    if not isinstance(value, (bytes, bytearray)) or len(value) != expected:
        raise CryptoError(f"{name} must be exactly {expected} bytes")


def generate_data_key() -> bytes:
    """A fresh random 256-bit data-encryption key (DEK) for one transfer."""

    return sodium.randombytes(DATA_KEY_BYTES)


def generate_nonce_prefix() -> bytes:
    """A fresh random 128-bit nonce prefix for one transfer."""

    return sodium.randombytes(NONCE_PREFIX_BYTES)


def _chunk_nonce(nonce_prefix: bytes, index: int) -> bytes:
    _require_length("nonce_prefix", nonce_prefix, NONCE_PREFIX_BYTES)
    if not isinstance(index, int) or index < 0 or index >= 2**64:
        raise CryptoError("chunk index must be a non-negative uint64")
    return nonce_prefix + struct.pack(">Q", index)


def _header_nonce(nonce_prefix: bytes) -> bytes:
    _require_length("nonce_prefix", nonce_prefix, NONCE_PREFIX_BYTES)
    return nonce_prefix + _HEADER_NONCE_SUFFIX


def _aad(*, transfer_id: bytes, role: str, index: int, chunk_count: int, plain_size: int) -> bytes:
    _require_length("transfer_id", transfer_id, 16)
    mapping = {
        0: _AAD_VERSION,
        1: transfer_id,
        2: role,
        3: index,
        4: chunk_count,
        5: plain_size,
    }
    return cbor2.dumps(mapping, canonical=True)


@dataclass(frozen=True)
class ChunkPlan:
    """How a plaintext of ``plain_size`` bytes is divided into chunks.

    Pure arithmetic, no I/O - callers stream plaintext through
    ``iter_chunk_bounds`` rather than loading a whole file into memory to
    encrypt it (design spec 14's streaming requirement for large files).
    """

    plain_size: int
    chunk_count: int

    @classmethod
    def for_size(cls, plain_size: int, chunk_size: int = CHUNK_SIZE_BYTES) -> "ChunkPlan":
        if plain_size < 0:
            raise CryptoError("plain_size must be >= 0")
        if chunk_size <= 0:
            raise CryptoError("chunk_size must be > 0")
        chunk_count = max(1, -(-plain_size // chunk_size)) if plain_size > 0 else 1
        return cls(plain_size=plain_size, chunk_count=chunk_count)

    def bounds(self, index: int, chunk_size: int = CHUNK_SIZE_BYTES):
        if index < 0 or index >= self.chunk_count:
            raise CryptoError(f"chunk index {index} out of range for {self.chunk_count} chunks")
        start = index * chunk_size
        end = min(start + chunk_size, self.plain_size)
        return start, end


def encrypt_chunk(
    *,
    data_key: bytes,
    nonce_prefix: bytes,
    transfer_id: bytes,
    index: int,
    chunk_count: int,
    plain_size: int,
    plaintext: bytes,
) -> bytes:
    """Encrypt one chunk. Returns ciphertext+tag (no nonce prefixed - the
    nonce is deterministic from ``nonce_prefix``/``index`` and is never
    stored per-chunk)."""

    _require_length("data_key", data_key, DATA_KEY_BYTES)
    nonce = _chunk_nonce(nonce_prefix, index)
    aad = _aad(transfer_id=transfer_id, role=_ROLE_CHUNK, index=index, chunk_count=chunk_count, plain_size=plain_size)
    return sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(bytes(plaintext), aad, nonce, data_key)


def decrypt_chunk(
    *,
    data_key: bytes,
    nonce_prefix: bytes,
    transfer_id: bytes,
    index: int,
    chunk_count: int,
    plain_size: int,
    ciphertext: bytes,
) -> bytes:
    """Decrypt and authenticate one chunk. Raises ``CryptoError`` (never a
    bare ``nacl.exceptions.CryptoError``) if the tag or AAD don't match -
    a receiver must treat that as "reject this chunk", not a crash."""

    _require_length("data_key", data_key, DATA_KEY_BYTES)
    nonce = _chunk_nonce(nonce_prefix, index)
    aad = _aad(transfer_id=transfer_id, role=_ROLE_CHUNK, index=index, chunk_count=chunk_count, plain_size=plain_size)
    try:
        return sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(bytes(ciphertext), aad, nonce, data_key)
    except Exception as exc:  # noqa: BLE001 - any libsodium failure means "reject", not "crash"
        raise CryptoError(f"chunk {index} failed AEAD verification") from exc


def encrypt_header(
    *,
    data_key: bytes,
    nonce_prefix: bytes,
    transfer_id: bytes,
    chunk_count: int,
    plain_size: int,
    header_plaintext: bytes,
) -> bytes:
    """Encrypt the plaintext manifest header. Returns ciphertext+tag; the
    nonce is deterministic (see module docstring) and is reconstructed by
    the caller from ``nonce_prefix`` when decrypting - callers that need to
    store a nonce field alongside this (ADR-0006's manifest blob schema
    does, for readability/forward-compatibility) can call
    ``header_nonce()`` directly."""

    _require_length("data_key", data_key, DATA_KEY_BYTES)
    nonce = _header_nonce(nonce_prefix)
    aad = _aad(
        transfer_id=transfer_id, role=_ROLE_HEADER, index=0, chunk_count=chunk_count, plain_size=plain_size
    )
    return sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(bytes(header_plaintext), aad, nonce, data_key)


def decrypt_header(
    *,
    data_key: bytes,
    nonce_prefix: bytes,
    transfer_id: bytes,
    chunk_count: int,
    plain_size: int,
    ciphertext: bytes,
) -> bytes:
    _require_length("data_key", data_key, DATA_KEY_BYTES)
    nonce = _header_nonce(nonce_prefix)
    aad = _aad(
        transfer_id=transfer_id, role=_ROLE_HEADER, index=0, chunk_count=chunk_count, plain_size=plain_size
    )
    try:
        return sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(bytes(ciphertext), aad, nonce, data_key)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("header failed AEAD verification") from exc


def header_nonce(nonce_prefix: bytes) -> bytes:
    """The deterministic 24-byte nonce used for the header ciphertext -
    exposed so the manifest blob can store it explicitly (ADR-0006's
    schema includes it for readability, even though it's re-derivable)."""

    return _header_nonce(nonce_prefix)
