"""meshsrv/attachments/mime_allowlist.py

MVP MIME allowlist (design spec section 20.2: "JPEG, PNG, WebP" explicit,
plus PDF/TXT/LOG/CSV/JSON named in the Execution Plan's Step 0.6 summary of
that section). This is an allowlist, not a denylist: anything not listed
here is rejected, including every other image/video/audio/archive type,
even common-and-usually-safe ones - MVP scope is deliberately narrow.

This module only says which MIME types/extensions are *permitted*; it is
not itself the MIME-sniffing step (design spec section 20.1, "MIME
spoofing" row: "Magic-byte sniff, extension не является источником
доверия") - a later step must derive `mime_type` from real content
inspection before calling `is_allowed_mime_type`, never from a client- or
extension-supplied claim alone.
"""

from __future__ import annotations

ALLOWED_MIME_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/webp",
        "application/pdf",
        "text/plain",  # covers both TXT and LOG - see ALLOWED_EXTENSIONS
        "text/csv",
        "application/json",
    }
)

# UI-facing extension list (for file pickers / drag-drop pre-filtering
# only). LOG has no MIME type of its own distinct from text/plain, so it
# is listed here but not in ALLOWED_MIME_TYPES.
ALLOWED_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".pdf", ".txt", ".log", ".csv", ".json"})


def sniff_mime_type(head_bytes: bytes) -> Optional[str]:
    """Derive a MIME type from the leading bytes of a file by content
    inspection (magic bytes for the binary formats; a decode/NUL heuristic
    for the text formats), never from a client-supplied Content-Type or
    filename extension (design spec section 20.1 "MIME spoofing": the
    extension is not a source of trust). Returns a string in
    `ALLOWED_MIME_TYPES` on a positive match, or `None` for anything not
    recognized as one of the allowlisted types - the caller must treat
    `None` as "not allowed", never as "trust the client's claim".

    The binary signatures are the canonical ones:

    - ``image/jpeg``: ``FF D8 FF`` (SOI marker);
    - ``image/png``: the 8-byte PNG signature ``89 50 4E 47 0D 0A 1A 0A``;
    - ``image/webp``: ``RIFF`` ... ``WEBP`` (the first 4 bytes are the
      ASCII ``RIFF`` chunk id and bytes 8-11 the ASCII ``WEBP`` fourCC);
    - ``application/pdf``: the ``%PDF-`` magic prefix.

    The text formats have no magic byte, so they fall through to a
    deterministic heuristic over the supplied head bytes: a leading NUL or
    a UTF-8 decode failure means "binary, not a known safe text type"
    (``None``); a leading ``{``/``[`` means ``application/json``; a comma
    in the first line means ``text/csv``; anything else that decoded
    cleanly is ``text/plain`` (which also covers ``.log``). The heuristic
    is best-effort *between* allowlisted text types, not a security
    boundary in itself - the boundary is the allowlist check that follows.
    It is deterministic (same bytes -> same result), which is what the
    canonical-hash idempotency computation depends on.
    """
    if head_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(head_bytes) >= 12 and head_bytes[0:4] == b"RIFF" and head_bytes[8:12] == b"WEBP":
        return "image/webp"
    if head_bytes.startswith(b"%PDF-"):
        return "application/pdf"
    if b"\x00" in head_bytes:
        return None
    try:
        head_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    stripped = head_bytes.lstrip()
    if stripped[:1] in (b"{", b"["):
        return "application/json"
    if b"," in head_bytes.split(b"\n", 1)[0]:
        return "text/csv"
    return "text/plain"


def is_allowed_mime_type(mime_type: str) -> bool:
    """`mime_type` must already be the result of magic-byte sniffing, not
    a client-supplied Content-Type header or a guess from the file
    extension - see the module docstring."""
    return mime_type in ALLOWED_MIME_TYPES


def is_allowed_extension(file_name: str) -> bool:
    """Advisory only (UI pre-filtering) - never a substitute for
    `is_allowed_mime_type` on sniffed content before the file is trusted."""
    lowered = file_name.lower()
    return any(lowered.endswith(ext) for ext in ALLOWED_EXTENSIONS)
