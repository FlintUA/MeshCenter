"""Subkeys of the storage key (ADR-0013 decision 2): file encryption, database fields, key files and the header
check never share a key.

libsodium's ``crypto_kdf`` is not exposed by PyNaCl; the construction it uses is keyed BLAKE2b with a personalization
string, which is exactly what is called here (``crypto_generichash_blake2b_salt_personal``)."""
import nacl.bindings as _sodium

from meshsrv.attachments.vault.errors import VaultError

STORAGE_KEY_BYTES = 32
_PURPOSES = {
    "files": b"mcv/files",
    "fields": b"mcv/fields",
    "keys": b"mcv/keys",
    "check": b"mcv/check",
}


def subkey(storage_key: bytes, purpose: str) -> bytes:
    if len(storage_key) != STORAGE_KEY_BYTES:
        raise VaultError("invalid_key")
    person = _PURPOSES.get(purpose)
    if person is None:
        raise VaultError("invalid_purpose")
    return _sodium.crypto_generichash_blake2b_salt_personal(
        b"", digest_size=32, key=bytes(storage_key), salt=bytes(16), person=person.ljust(16, bytes(1))
    )
