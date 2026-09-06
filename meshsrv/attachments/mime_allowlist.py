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
