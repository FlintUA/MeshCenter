"""Passphrase normalization and the strength rule (ADR-0013 decision 7): at least 12 characters, with a visible
strength hint, and no composition rules."""
import unicodedata

MIN_LENGTH = 12

# A deliberately small list of the passwords that are everywhere; the hint says "common", it never blocks beyond
# the length rule except for these exact entries (a 12+ character passphrase that is on this list is refused).
_COMMON = frozenset({
    "passwordpassword", "password1234", "123456789012", "qwertyuiop12", "qwertyuiopasdf", "iloveyouiloveyou",
    "administrator", "letmeinletmein", "welcomewelcome", "1234567890123", "123456789abc", "abcdefghijkl",
    "meshcentermeshcenter", "meshtasticmeshtastic", "changemechangeme",
})


def normalize(passphrase: str) -> bytes:
    """NFKC then UTF-8, so the same passphrase typed on another keyboard layout or platform matches. The result is
    what the KDF sees; the caller drops its reference to the ``str`` at once (Python cannot zero it)."""
    return unicodedata.normalize("NFKC", passphrase).encode("utf-8")


def assess(passphrase: str) -> dict:
    """``{"ok": bool, "level": "too_short" | "common" | "weak" | "fair" | "good", "length": int}``.

    ``ok`` is the rule that is enforced: length of at least 12 code points and not a known-common value. ``level``
    is only the hint shown while typing (length and variety), never a reason to refuse by itself."""
    text = unicodedata.normalize("NFKC", passphrase)
    length = len(text)
    if length < MIN_LENGTH:
        return {"ok": False, "level": "too_short", "length": length}
    if text.lower().replace(" ", "") in _COMMON or len(set(text)) <= 3:
        return {"ok": False, "level": "common", "length": length}
    classes = sum([
        any(c.islower() for c in text), any(c.isupper() for c in text),
        any(c.isdigit() for c in text), any(not c.isalnum() for c in text),
    ])
    words = len(text.split())
    if length >= 20 or (length >= 16 and (classes >= 3 or words >= 3)):
        level = "good"
    elif length >= 14 and (classes >= 2 or words >= 2):
        level = "fair"
    else:
        level = "weak"
    return {"ok": True, "level": level, "length": length}
