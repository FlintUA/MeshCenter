"""The recovery key: 32 random bytes, shown once as 13 groups of 4 base32 characters (52 characters carry the full
256 bits; the last character has four spare bits that must be zero). Never stored; only a wrapped copy of the
storage key is in the vault header."""
import base64

from meshsrv.attachments.vault.errors import VaultError

RECOVERY_BYTES = 32
_GROUPS = 13


def encode(recovery_key: bytes) -> str:
    if len(recovery_key) != RECOVERY_BYTES:
        raise VaultError("invalid_recovery_key")
    text = base64.b32encode(bytes(recovery_key)).decode("ascii").rstrip("=")
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def decode(text: str) -> bytes:
    """Accepts lower case, spaces and dashes. Anything else, a wrong length or non-zero spare bits is refused."""
    cleaned = "".join(ch for ch in str(text).upper() if ch not in " -\t\r\n")
    if len(cleaned) != _GROUPS * 4 or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for ch in cleaned):
        raise VaultError("invalid_recovery_key")
    raw = base64.b32decode(cleaned + "====")
    if len(raw) != RECOVERY_BYTES or encode(raw).replace("-", "") != cleaned:
        raise VaultError("invalid_recovery_key")
    return raw


def groups(text: str) -> list:
    """The groups of a displayed key, for the "type back two groups" confirmation."""
    return str(text).split("-")
