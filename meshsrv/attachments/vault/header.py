"""vault.json: the storage key wrapped twice, by the passphrase and by the recovery key (ADR-0013 decision 2).

    { "v": 1,
      "kdf": {"alg": "argon2id13", "opslimit": n, "memlimit": n, "salt": b64, "measured_ms": n},
      "wrap_pass":     {"nonce": b64, "ct": b64},
      "wrap_recovery": {"nonce": b64, "ct": b64},
      "check": b64 }                                  # nonce || ciphertext of a constant under a subkey of SK

The storage key (SK) is 32 random bytes and is never derived from the passphrase, so changing the passphrase re-wraps
SK and never touches data. The KDF parameters and salt are bound into the associated data of the passphrase wrap: a
header whose parameters were edited does not open. The floor is checked on read. A wrong passphrase and a damaged
header both end in the same ``unlock_failed``.
"""
import base64
import json
from dataclasses import dataclass
from typing import Tuple

import nacl.bindings as _sodium
from nacl.utils import random as _random

from meshsrv.attachments.vault import kdf
from meshsrv.attachments.vault.errors import VaultError
from meshsrv.attachments.vault.keys import STORAGE_KEY_BYTES, subkey

FORMAT_VERSION = 1
_NONCE_BYTES = _sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES     # 24
_SEALED_KEY_BYTES = STORAGE_KEY_BYTES + _sodium.crypto_aead_xchacha20poly1305_ietf_ABYTES
_CHECK_PLAINTEXT = b"mca-vault-check-v1"
_AD_PASS = b"MCV1-HDR|pass|"
_AD_RECOVERY = b"MCV1-HDR|recovery"
_AD_CHECK = b"MCV1-HDR|check"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("=")


def _unb64(text, expected_len=None) -> bytes:
    if not isinstance(text, str):
        raise VaultError("invalid_header")
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise VaultError("invalid_header")
    if expected_len is not None and len(raw) != expected_len:
        raise VaultError("invalid_header")
    return raw


def _seal(key: bytes, plaintext: bytes, ad: bytes) -> Tuple[bytes, bytes]:
    nonce = _random(_NONCE_BYTES)
    return nonce, _sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(bytes(plaintext), ad, nonce, bytes(key))


def _open(key: bytes, nonce: bytes, ciphertext: bytes, ad: bytes) -> bytes:
    try:
        return _sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(bytes(ciphertext), ad, bytes(nonce), bytes(key))
    except Exception:  # one answer for every failure: a wrong key and a damaged header are not told apart
        raise VaultError("unlock_failed")


def _zero(buf: bytearray) -> None:
    for i in range(len(buf)):
        buf[i] = 0


@dataclass(frozen=True)
class VaultHeader:
    kdf_params: kdf.KdfParams
    salt: bytes
    wrap_pass_nonce: bytes
    wrap_pass_ct: bytes
    wrap_recovery_nonce: bytes
    wrap_recovery_ct: bytes
    check: bytes

    def to_json(self) -> str:
        return json.dumps({
            "v": FORMAT_VERSION,
            "kdf": {
                "alg": "argon2id13", "opslimit": self.kdf_params.opslimit, "memlimit": self.kdf_params.memlimit,
                "salt": _b64(self.salt), "measured_ms": self.kdf_params.measured_ms,
            },
            "wrap_pass": {"nonce": _b64(self.wrap_pass_nonce), "ct": _b64(self.wrap_pass_ct)},
            "wrap_recovery": {"nonce": _b64(self.wrap_recovery_nonce), "ct": _b64(self.wrap_recovery_ct)},
            "check": _b64(self.check),
        }, indent=2, sort_keys=True)

    @staticmethod
    def from_json(text: str) -> "VaultHeader":
        """Strict parse; the KDF floor is enforced here, before anything can be derived from the header."""
        try:
            doc = json.loads(text)
        except (ValueError, TypeError):
            raise VaultError("invalid_header")
        if not isinstance(doc, dict):
            raise VaultError("invalid_header")
        if doc.get("v") != FORMAT_VERSION:
            raise VaultError("unsupported_header_version" if "v" in doc else "invalid_header")
        try:
            kdf_doc, wp, wr = doc["kdf"], doc["wrap_pass"], doc["wrap_recovery"]
            if kdf_doc["alg"] != "argon2id13":
                raise VaultError("invalid_header")
            params = kdf.validate_params(kdf.KdfParams(
                opslimit=kdf_doc["opslimit"], memlimit=kdf_doc["memlimit"],
                measured_ms=int(kdf_doc.get("measured_ms", 0) or 0)))
            return VaultHeader(
                kdf_params=params,
                salt=_unb64(kdf_doc["salt"], kdf.SALT_BYTES),
                wrap_pass_nonce=_unb64(wp["nonce"], _NONCE_BYTES), wrap_pass_ct=_unb64(wp["ct"], _SEALED_KEY_BYTES),
                wrap_recovery_nonce=_unb64(wr["nonce"], _NONCE_BYTES), wrap_recovery_ct=_unb64(wr["ct"], _SEALED_KEY_BYTES),
                check=_unb64(doc["check"]),
            )
        except VaultError:
            raise
        except (KeyError, TypeError, ValueError):
            raise VaultError("invalid_header")


def _ad_pass(params: kdf.KdfParams, salt: bytes) -> bytes:
    # measured_ms is informational and not bound; everything that decides the derivation is
    return _AD_PASS + json.dumps(
        {"alg": "argon2id13", "opslimit": params.opslimit, "memlimit": params.memlimit, "salt": _b64(salt)},
        sort_keys=True, separators=(",", ":")).encode("ascii")


def _recovery_wrap_key(recovery_key: bytes) -> bytes:
    return _sodium.crypto_generichash_blake2b_salt_personal(
        b"", digest_size=32, key=bytes(recovery_key), salt=bytes(16), person=b"mcv/recovery-wra")


def _make_check(storage_key: bytes) -> bytes:
    nonce, ct = _seal(subkey(storage_key, "check"), _CHECK_PLAINTEXT, _AD_CHECK)
    return nonce + ct


def _verify_check(storage_key: bytes, check: bytes) -> None:
    if len(check) <= _NONCE_BYTES:
        raise VaultError("unlock_failed")
    if _open(subkey(storage_key, "check"), check[:_NONCE_BYTES], check[_NONCE_BYTES:], _AD_CHECK) != _CHECK_PLAINTEXT:
        raise VaultError("unlock_failed")


def _wrap_with_passphrase(storage_key: bytes, passphrase: bytes, params: kdf.KdfParams) -> Tuple[bytes, bytes, bytes]:
    salt = kdf.new_salt()
    wrap_key = kdf.derive_key(passphrase, salt, params)
    try:
        nonce, ct = _seal(bytes(wrap_key), storage_key, _ad_pass(params, salt))
    finally:
        _zero(wrap_key)
    return salt, nonce, ct


@dataclass
class CreatedVault:
    header: VaultHeader
    storage_key: bytearray      # the caller keeps it in a SecretBuffer and zeroes it
    recovery_key: bytes         # shown once, never stored


def create(passphrase: bytes, params: kdf.KdfParams) -> CreatedVault:
    """A new vault: random SK, wrapped by the passphrase and by a fresh random recovery key."""
    kdf.validate_params(params)
    storage_key = bytearray(_random(STORAGE_KEY_BYTES))
    recovery_key = _random(32)
    salt, pass_nonce, pass_ct = _wrap_with_passphrase(bytes(storage_key), passphrase, params)
    rec_nonce, rec_ct = _seal(_recovery_wrap_key(recovery_key), bytes(storage_key), _AD_RECOVERY)
    header = VaultHeader(params, salt, pass_nonce, pass_ct, rec_nonce, rec_ct, _make_check(bytes(storage_key)))
    return CreatedVault(header=header, storage_key=storage_key, recovery_key=recovery_key)


def unlock(header: VaultHeader, passphrase: bytes) -> bytearray:
    """SK from the passphrase. Raises ``unlock_failed`` for a wrong passphrase and for any damage alike."""
    kdf.validate_params(header.kdf_params)
    wrap_key = kdf.derive_key(passphrase, header.salt, header.kdf_params)
    try:
        storage_key = _open(bytes(wrap_key), header.wrap_pass_nonce, header.wrap_pass_ct,
                            _ad_pass(header.kdf_params, header.salt))
    finally:
        _zero(wrap_key)
    _verify_check(storage_key, header.check)
    return bytearray(storage_key)


def unlock_with_recovery(header: VaultHeader, recovery_key: bytes) -> bytearray:
    if len(recovery_key) != 32:
        raise VaultError("invalid_recovery_key")
    storage_key = _open(_recovery_wrap_key(recovery_key), header.wrap_recovery_nonce, header.wrap_recovery_ct, _AD_RECOVERY)
    _verify_check(storage_key, header.check)
    return bytearray(storage_key)


def change_passphrase(header: VaultHeader, storage_key: bytes, new_passphrase: bytes, params: kdf.KdfParams) -> VaultHeader:
    """Re-wrap SK under a new passphrase and/or new parameters (also what "re-calibrate" does). Fresh salt, the
    recovery wrap is kept, no data is touched."""
    _verify_check(storage_key, header.check)
    salt, nonce, ct = _wrap_with_passphrase(bytes(storage_key), new_passphrase, kdf.validate_params(params))
    return VaultHeader(params, salt, nonce, ct, header.wrap_recovery_nonce, header.wrap_recovery_ct, header.check)


def new_recovery_key(header: VaultHeader, storage_key: bytes) -> Tuple[VaultHeader, bytes]:
    """Issue a new recovery key; the old one stops working. Shown once by the caller."""
    _verify_check(storage_key, header.check)
    recovery_key = _random(32)
    nonce, ct = _seal(_recovery_wrap_key(recovery_key), bytes(storage_key), _AD_RECOVERY)
    return VaultHeader(header.kdf_params, header.salt, header.wrap_pass_nonce, header.wrap_pass_ct, nonce, ct,
                       header.check), recovery_key
