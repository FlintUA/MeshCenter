"""A gallery picture prepared for sending through MCAttach (gallery G2).

`prepare(path, size_key)` returns the JPEG bytes the browser then uploads with the ordinary `POST /api/attachments`:

- `"1280"` / `"1920"`: the long edge is cut down to that size (Pillow, EXIF orientation applied, re-encoded JPEG quality 82,
  no metadata). A picture that is already that small is not re-encoded (no quality loss): only its metadata is removed.
- `"original"`: the file as it is, but without EXIF / XMP / IPTC / comment segments (a lossless cut of the JPEG segments, the
  picture data is not touched). Only within the 5 MiB MCAttach limit: a larger one is refused with `too_large`.

Nothing here writes a file or keeps a copy; the original in the gallery is never changed.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

MAX_SEND_BYTES = 5 * 1024 * 1024  # mirrors _MAX_FILE_BYTES in api/api_attachments.py
JPEG_QUALITY = 82
READ_LIMIT_BYTES = 64 * 1024 * 1024
SEND_SIZES = {"1280": 1280, "1920": 1920, "original": None}
# APP1 (EXIF, XMP), APP13 (IPTC / Photoshop) and COM segments carry metadata (a GPS position among it); everything else
# (JFIF, the ICC colour profile, Adobe colour transform, tables, the picture itself) stays
_DROPPED_MARKERS = frozenset({0xE1, 0xED, 0xFE})


class SendCopyError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def strip_jpeg_metadata(data: bytes) -> bytes:
    """The same JPEG without its metadata segments (lossless: the entropy-coded picture data is copied as it is)."""
    if data[:2] != b"\xff\xd8":
        raise SendCopyError("invalid_image", "This is not a JPEG picture.")
    out = bytearray(b"\xff\xd8")
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            raise SendCopyError("invalid_image", "The JPEG picture is damaged.")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA:  # start of scan: the rest is picture data up to the end of the file
            out += data[i:]
            return bytes(out)
        if marker in (0x01,) or 0xD0 <= marker <= 0xD9:  # markers without a length
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if length < 2 or i + 2 + length > n:
            raise SendCopyError("invalid_image", "The JPEG picture is damaged.")
        if marker not in _DROPPED_MARKERS:
            out += data[i:i + 2 + length]
        i += 2 + length
    raise SendCopyError("invalid_image", "The JPEG picture is damaged.")


def _check_size(data: bytes) -> bytes:
    if len(data) > MAX_SEND_BYTES:
        raise SendCopyError("too_large", "The picture is larger than the 5 MB limit of a transfer.",
                            size_bytes=len(data), max_bytes=MAX_SEND_BYTES)
    return data


def prepare(path: str, size_key: str) -> tuple[bytes, tuple[int, int]]:
    """(jpeg bytes, (width, height)) for sending; raises SendCopyError."""
    if size_key not in SEND_SIZES:
        raise SendCopyError("invalid_size", "Choose 1280, 1920 or original.")
    limit = SEND_SIZES[size_key]
    try:
        with open(path, "rb") as handle:
            raw = handle.read(READ_LIMIT_BYTES)  # a gallery picture is far smaller; this only bounds a broken file
        with Image.open(io.BytesIO(raw)) as probe:
            probe.load()
            width, height = probe.size
    except (OSError, ValueError) as error:
        raise SendCopyError("invalid_image", "The picture could not be read.") from error
    if limit is None or max(width, height) <= limit:
        # nothing to shrink: keep the pixels exactly, take the metadata off
        return _check_size(strip_jpeg_metadata(raw)), (width, height)
    with Image.open(io.BytesIO(raw)) as source:
        image = ImageOps.exif_transpose(source)  # the orientation is applied, then the tag is gone with all the others
        image = image.convert("RGB")
        scale = limit / float(max(image.size))
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)  # no exif= argument: nothing is carried over
    data = _check_size(buffer.getvalue())
    return data, image.size
