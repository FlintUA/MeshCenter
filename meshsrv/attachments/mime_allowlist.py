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

import codecs
import json
from typing import Optional

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


# ---- Finding 8: content-first filename normalization ----------------------
#
# The create endpoint establishes MIME from content (magic bytes), never from
# a browser Content-Type or a filename extension, and must then make the
# recorded filename's extension *consistent* with that content MIME so the
# worker's later independent `is_allowed_extension` check can never reject an
# already-accepted request (`sender._step_validating` applies the allowlist a
# second time, with an `extension_not_allowed` failure). The extension is only
# ever checked/normalized *after* content MIME is established, per design spec
# section 20.1 - never used to choose the MIME in the first place.

# MIME types whose content is text and must therefore survive the full-stream
# UTF-8/NUL validation (Finding 8). Binary formats (JPEG/PNG/WebP/PDF) are
# excluded: they are identified by magic bytes and legitimately contain NUL
# and non-UTF-8 bytes.
TEXT_FAMILY_MIME_TYPES = frozenset({"text/plain", "text/csv", "application/json"})

# MIME type -> the filename extensions that are consistent with it, canonical
# (first) extension first. `.log` has no distinct MIME type, so it shares
# `text/plain`'s entry.
_MIME_EXTENSIONS = {
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
    "image/webp": (".webp",),
    "application/pdf": (".pdf",),
    "text/plain": (".txt", ".log"),
    "text/csv": (".csv",),
    "application/json": (".json",),
}


def allowed_extensions_for_mime(mime_type: str) -> "tuple[str, ...]":
    """The filename extensions consistent with `mime_type`, canonical-first,
    or `()` for an unknown/non-allowlisted type."""
    return _MIME_EXTENSIONS.get(mime_type, ())


def canonical_extension(mime_type: str) -> Optional[str]:
    """The canonical (first) extension for `mime_type`, or `None` for an
    unknown type."""
    extensions = _MIME_EXTENSIONS.get(mime_type)
    return extensions[0] if extensions else None


# The worker re-validates `source_name` with `_bounded_text(..., max_len=255)`,
# so the *final* normalized name (extension included) must never exceed this
# many Unicode code points or the route would accept a name the worker rejects
# as `invalid_payload` (§7.2, Finding 8).
MAX_SOURCE_NAME_CODE_POINTS = 255


def _truncate_code_points(text: str, max_code_points: Optional[int]) -> str:
    """Truncate `text` to at most `max_code_points` Unicode code points, or
    return it unchanged when the cap is `None`. Python `str` slicing is
    code-point-safe (never splits a multi-byte character), so multibyte names
    are measured by character count, not UTF-8 byte length."""
    if max_code_points is None:
        return text
    if max_code_points <= 0:
        return ""
    return text[:max_code_points]


def sanitize_display_name(raw_filename) -> str:
    """Derive a safe, single-component display name from an untrusted
    filename (a manifest header `file_name` from a possibly hostile sender,
    or any client-supplied name), for use as a `files/` name and as a
    `Content-Disposition` filename. Never trusts the input as a path: takes
    the basename (so a hostile `../../etc/passwd` or `..\\..\\x` cannot
    survive even as a display name), drops non-printable characters (which
    also strips CR/LF - no header injection), strips leading/trailing dots
    and whitespace, and falls back to `"attachment"` when nothing safe
    remains. The result is guaranteed NUL-free, path-separator-free,
    non-empty, and not `.`/`..` - safe to hand to
    `workspace.resolve_saved_path()` or to embed in a filename. Length is
    deliberately *not* capped here - callers that need a bounded name
    truncate afterwards (`normalize_file_name_for_mime(..., max_code_
    points=...)` on the create path; `MAX_SOURCE_NAME_CODE_POINTS` on the
    save path)."""
    if not isinstance(raw_filename, str):
        return "attachment"
    base = raw_filename.replace("\\", "/").rsplit("/", 1)[-1]
    base = "".join(ch for ch in base if ch.isprintable())
    base = base.strip().strip(".")
    if not base:
        return "attachment"
    return base


def normalize_file_name_for_mime(
    file_name: str, mime_type: str, max_code_points: Optional[int] = None
) -> str:
    """Make `file_name`'s extension consistent with `mime_type`, preserving an
    already-consistent extension and otherwise replacing/appending the
    canonical one. `mime_type` is already established from content by the
    caller; the extension is checked case-insensitively but never used to
    *choose* the MIME.

    Examples: ``app.log`` + ``text/plain`` -> ``app.log`` (kept);
    ``photo.jpg`` + ``image/jpeg`` -> ``photo.jpg`` (kept); ``report.exe`` +
    ``text/plain`` -> ``report.txt`` (replaced); ``notes`` + ``text/plain`` ->
    ``notes.txt`` (appended). This guarantees the worker's later
    `is_allowed_extension(file_name)` always passes for an accepted request,
    closing the Finding 8 gap where a request accepted here could otherwise
    fail later in `sender._step_validating` on an independent extension rule.

    When `max_code_points` is set, the *final* name (extension included) is
    capped to that many code points by truncating only the stem, never the
    extension - so an overlong name keeps its (existing or canonical)
    extension and the result always stays within the worker's 255-code-point
    `source_name` bound rather than being truncated *then* extended (which
    could exceed the bound).
    """
    extensions = _MIME_EXTENSIONS.get(mime_type)
    if extensions is None:
        return _truncate_code_points(file_name, max_code_points)
    lowered = file_name.lower()
    for ext in extensions:
        if lowered.endswith(ext):
            # Already consistent: keep the name and its exact (case-preserved)
            # extension, capping only the stem.
            stem = file_name[: -len(ext)]
            tail = file_name[-len(ext):]
            stem_cap = None if max_code_points is None else max_code_points - len(ext)
            return _truncate_code_points(stem, stem_cap) + tail
    canonical = extensions[0]
    if "." in file_name:
        stem, _ = file_name.rsplit(".", 1)
    else:
        stem = file_name
    stem_cap = None if max_code_points is None else max_code_points - len(canonical)
    stem = _truncate_code_points(stem, stem_cap)
    return stem + canonical


class TextStreamValidator:
    """Incremental full-stream text validation (Finding 8): fed each staging
    chunk, it detects a NUL byte anywhere in the stream and any invalid UTF-8
    sequence - including a multi-byte sequence split across chunk boundaries -
    so a text candidate whose binary/invalid content only appears *after* the
    sniffed 512-byte head is still rejected. Cheap enough to run on every file
    during staging; the caller only enforces the result for
    `TEXT_FAMILY_MIME_TYPES` (binary formats legitimately contain NUL and
    non-UTF-8 bytes, so their result is ignored)."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._clean = True

    def feed(self, chunk: bytes) -> None:
        if not self._clean:
            return
        if b"\x00" in chunk:
            self._clean = False
            return
        try:
            self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError:
            self._clean = False

    def finish(self) -> bool:
        """Flush any trailing partial sequence and report whether the whole
        stream was clean UTF-8 with no NUL byte. Idempotent."""
        if not self._clean:
            return False
        try:
            self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            self._clean = False
            return False
        return True


def validate_json_document(path) -> None:
    """Validate that the already-staged file at `path` (a `str`/path-like) is
    one complete JSON document (Finding 8: a leading `{`/`[` in the head is
    not enough - the whole document must parse). Raises `ValueError` (incl.
    `json.JSONDecodeError`) on invalid JSON. Bounded by the create endpoint's
    5 MiB cap and only called for `application/json`."""
    with open(path, "r", encoding="utf-8") as fh:
        json.load(fh)
