"""Encrypted files (``<attachment-id>.mcv``): libsodium secretstream in 64 KiB chunks (ADR-0013 section 4).

Layout:  ``MCV1`` | format byte (1) | 24-byte secretstream header | chunk | chunk | ... | final chunk

Every chunk is one ``crypto_secretstream_xchacha20poly1305`` message of at most 64 KiB of plaintext (17 bytes of tag
and MAC on top), with associated data ``MCV1|<attachment-id>``; the last message carries ``TAG_FINAL`` (an empty file
is one empty final message). Consequences the tests pin:

* a chunk copied into another attachment's file fails (the associated data differs);
* reordering or dropping chunks fails (the stream state is positional);
* truncation fails (no final tag), and so does anything appended after the final chunk;
* the key is the ``files`` subkey of the storage key, and the per-file stream header is random.

Reading streams chunk by chunk: nothing is ever held beyond one chunk, and no decrypted temporary file is written.
"""
import hashlib
import math
import os
from typing import BinaryIO, Iterator, Optional

import nacl.bindings as _sodium

from meshsrv.attachments.vault.errors import VaultError

MAGIC = b"MCV1"
FORMAT_BYTE = 1
CHUNK = 64 * 1024
_HEADER_BYTES = _sodium.crypto_secretstream_xchacha20poly1305_HEADERBYTES     # 24
_ABYTES = _sodium.crypto_secretstream_xchacha20poly1305_ABYTES               # 17
_TAG_MESSAGE = _sodium.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
_TAG_FINAL = _sodium.crypto_secretstream_xchacha20poly1305_TAG_FINAL
_PREFIX = len(MAGIC) + 1 + _HEADER_BYTES
_CT_CHUNK = CHUNK + _ABYTES


def _ad(attachment_id: str) -> bytes:
    return b"MCV1|" + str(attachment_id).encode("utf-8")


def _read_full(src: BinaryIO, n: int) -> bytes:
    """Read up to n bytes, looping over short reads (a socket or pipe may return less than asked)."""
    parts = []
    remaining = n
    while remaining:
        block = src.read(remaining)
        if not block:
            break
        parts.append(block)
        remaining -= len(block)
    return b"".join(parts)


def encrypted_size(plain_size: int) -> int:
    """Exact size of the .mcv file for a plaintext of ``plain_size`` bytes."""
    chunks = max(1, math.ceil(plain_size / CHUNK))
    return _PREFIX + plain_size + chunks * _ABYTES


def plain_size(encrypted: int) -> Optional[int]:
    """The plaintext size of an .mcv file of ``encrypted`` bytes (for Content-Length), or None if it cannot be valid."""
    body = encrypted - _PREFIX
    if body < _ABYTES:
        return None
    chunks = math.ceil(body / _CT_CHUNK)
    size = body - chunks * _ABYTES
    return size if size >= 0 and encrypted_size(size) == encrypted else None


def encrypt_stream(src: BinaryIO, dst: BinaryIO, key: bytes, attachment_id: str) -> int:
    """Encrypt ``src`` to ``dst``. Returns the number of plaintext bytes. ``key`` is the ``files`` subkey."""
    if len(key) != 32:
        raise VaultError("invalid_key")
    state = _sodium.crypto_secretstream_xchacha20poly1305_state()
    header = _sodium.crypto_secretstream_xchacha20poly1305_init_push(state, bytes(key))
    dst.write(MAGIC + bytes([FORMAT_BYTE]) + header)
    ad = _ad(attachment_id)
    total = 0
    current = _read_full(src, CHUNK)
    while True:
        # read ahead one chunk so the last real chunk can carry TAG_FINAL (an exact multiple of the chunk size too)
        following = _read_full(src, CHUNK) if len(current) == CHUNK else b""
        final = not following
        dst.write(_sodium.crypto_secretstream_xchacha20poly1305_push(
            state, current, ad, _TAG_FINAL if final else _TAG_MESSAGE))
        total += len(current)
        if final:
            return total
        current = following


def decrypt_iter(src: BinaryIO, key: bytes, attachment_id: str) -> Iterator[bytes]:
    """Yield the plaintext chunk by chunk. Raises ``stream_corrupt`` on any authentication failure, and
    ``stream_truncated`` when the file ends before the final chunk. The generator must be run to its end (or
    closed) by the caller; a consumer that stops early has verified only what it read."""
    if len(key) != 32:
        raise VaultError("invalid_key")
    prefix = _read_full(src, _PREFIX)
    if len(prefix) < _PREFIX or prefix[:4] != MAGIC or prefix[4] != FORMAT_BYTE:
        raise VaultError("stream_corrupt")
    state = _sodium.crypto_secretstream_xchacha20poly1305_state()
    try:
        _sodium.crypto_secretstream_xchacha20poly1305_init_pull(state, prefix[5:], bytes(key))
    except Exception:
        raise VaultError("stream_corrupt")
    ad = _ad(attachment_id)
    while True:
        block = _read_full(src, _CT_CHUNK)
        if not block:
            raise VaultError("stream_truncated")          # ended without a final chunk
        try:
            plain, tag = _sodium.crypto_secretstream_xchacha20poly1305_pull(state, block, ad)
        except Exception:
            raise VaultError("stream_corrupt")
        if tag == _TAG_FINAL:
            if src.read(1):
                raise VaultError("stream_corrupt")        # data after the final chunk
            yield plain
            return
        if len(block) != _CT_CHUNK:
            raise VaultError("stream_truncated")          # a short chunk that is not final
        yield plain


def decrypt_bytes(data: bytes, key: bytes, attachment_id: str) -> bytes:
    import io
    return b"".join(decrypt_iter(io.BytesIO(data), key, attachment_id))


def encrypt_bytes(data: bytes, key: bytes, attachment_id: str) -> bytes:
    import io
    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, key, attachment_id)
    return out.getvalue()


def _create(path: str, mode: int):
    """A new binary file opened for writing with an explicit mode (a leftover file with another mode is fixed too)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), mode)
    try:
        os.chmod(path, mode)
    except OSError:
        pass
    return os.fdopen(fd, "wb")


def encrypt_file(src_path: str, dst_path: str, key: bytes, attachment_id: str, mode: int = 0o600) -> int:
    """Encrypt ``src_path`` to ``dst_path`` atomically: written to ``dst_path + '.part'``, fsynced, renamed. The
    plain source is NOT removed here (the migration verifies first, then deletes). The file is created with ``mode``
    (0600 unless the caller keeps the source's own mode), never with the umask's 0644. Returns the plaintext size."""
    part = dst_path + ".part"
    try:
        with open(src_path, "rb") as src, _create(part, mode) as dst:
            total = encrypt_stream(src, dst, key, attachment_id)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(part, dst_path)
        return total
    except BaseException:
        try:
            os.remove(part)
        except OSError:
            pass
        raise


def verify_file(enc_path: str, key: bytes, attachment_id: str, expected_sha256: str, expected_size: int) -> bool:
    """Decrypt ``enc_path`` in memory, chunk by chunk, and compare the SHA-256 and the size with the plain original.
    This is the check the migration runs before it deletes any plain file."""
    digest = hashlib.sha256()
    size = 0
    try:
        with open(enc_path, "rb") as handle:
            for chunk in decrypt_iter(handle, key, attachment_id):
                digest.update(chunk)
                size += len(chunk)
    except VaultError:
        return False
    return size == expected_size and digest.hexdigest() == str(expected_sha256).lower()
