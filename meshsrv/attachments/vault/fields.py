"""Encrypted database fields (ADR-0013 section 5): ``v1:<nonce>:<ciphertext>`` stored in the same TEXT column, so the
schema does not change.

XChaCha20-Poly1305 with the ``fields`` subkey and associated data ``table.column.row-id``: a value copied into another
row or column does not decrypt. A value that does not start with ``v1:`` is plain (a database that has not been
migrated yet, or a column the vault does not cover) and is returned as it is by ``read``.
"""
import base64
from typing import Optional

import nacl.bindings as _sodium
from nacl.utils import random as _random

from meshsrv.attachments.vault.errors import VaultError

PREFIX = "v1:"
_NONCE_BYTES = _sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES


def _ad(table: str, column: str, row_id: str) -> bytes:
    return ("%s.%s.%s" % (table, column, row_id)).encode("utf-8")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_encrypted(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_field(key: bytes, table: str, column: str, row_id: str, plaintext: str) -> str:
    if len(key) != 32:
        raise VaultError("invalid_key")
    nonce = _random(_NONCE_BYTES)
    ct = _sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext.encode("utf-8"), _ad(table, column, row_id), nonce, bytes(key))
    return PREFIX + _b64(nonce) + ":" + _b64(ct)


def decrypt_field(key: bytes, table: str, column: str, row_id: str, value: str) -> str:
    """Raises ``field_corrupt`` for a value that does not authenticate for this table, column and row."""
    if len(key) != 32:
        raise VaultError("invalid_key")
    if not is_encrypted(value):
        raise VaultError("field_corrupt")
    try:
        nonce_text, ct_text = value[len(PREFIX):].split(":", 1)
        nonce, ct = _unb64(nonce_text), _unb64(ct_text)
        if len(nonce) != _NONCE_BYTES:
            raise ValueError("nonce")
        return _sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(ct, _ad(table, column, row_id), nonce, bytes(key)).decode("utf-8")
    except Exception:
        raise VaultError("field_corrupt")


def read(key: Optional[bytes], table: str, column: str, row_id: str, value: Optional[str]) -> Optional[str]:
    """What the service shows for a stored value: plain text as it is, an encrypted value decrypted, and ``None``
    (the "locked" placeholder) when it is encrypted and there is no key."""
    if not is_encrypted(value):
        return value
    if key is None:
        return None
    return decrypt_field(key, table, column, row_id, value)
