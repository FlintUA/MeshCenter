"""Small preview pictures for the Files cards (FX1): a size-capped JPEG made on the fly from a verified image.

Nothing is stored: the picture is decoded in memory, shrunk and returned; under the vault the decrypted bytes exist only for
this call and the result is sent with `no-store`. Memory is bounded on purpose (a Raspberry Pi Zero 2 W has about 425 MB):
  - the bytes must be one of the three image types the Files list previews (jpeg, png, webp) AND their magic bytes must
    say so (the header MIME of a received file is sender-controlled);
  - a JPEG is decoded with Pillow's `draft()` (the decoder scales by 1/2, 1/4 or 1/8 while it reads, so a 12 MP photo
    never exists in full size in memory); other formats are refused above `MAX_OTHER_PIXELS`;
  - at most `MAX_CONCURRENT` thumbnails are made at the same time.
"""
import io
import threading
from typing import Optional

from meshsrv.attachments import mime_allowlist

THUMB_EDGE = 160                  # the longest edge of the result, in pixels
THUMB_QUALITY = 72
MAX_JPEG_PIXELS = 64_000_000      # decoded at a fraction of this size thanks to draft()
MAX_OTHER_PIXELS = 8_000_000      # png / webp are decoded in full: about 32 MB as RGBA
MAX_CONCURRENT = 2
WAIT_SECONDS = 8.0

_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
THUMBNAIL_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})


class ThumbnailError(Exception):
    """`code` is a stable machine-readable reason: not_an_image, type_mismatch, too_large, busy, undecodable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def make_thumbnail(data: bytes, declared_mime: Optional[str]) -> bytes:
    if declared_mime not in THUMBNAIL_TYPES:
        raise ThumbnailError("not_an_image")
    if mime_allowlist.sniff_mime_type(bytes(data[:64])) != declared_mime:
        raise ThumbnailError("type_mismatch")
    if not _slots.acquire(timeout=WAIT_SECONDS):
        raise ThumbnailError("busy")
    try:
        return _render(data, declared_mime)
    finally:
        _slots.release()


def _render(data: bytes, mime: str) -> bytes:
    from PIL import Image                    # Pillow is a dependency of the camera and the gallery already

    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        limit = MAX_JPEG_PIXELS if mime == "image/jpeg" else MAX_OTHER_PIXELS
        if width <= 0 or height <= 0 or width * height > limit:
            raise ThumbnailError("too_large")
        if mime == "image/jpeg":
            image.draft("RGB", (THUMB_EDGE * 2, THUMB_EDGE * 2))
        image.load()
        if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[3])
            image = flat
        elif image.mode != "RGB":
            image = image.convert("RGB")
        image.thumbnail((THUMB_EDGE, THUMB_EDGE))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=THUMB_QUALITY, optimize=True)
        return out.getvalue()
    except ThumbnailError:
        raise
    except Exception as error:   # noqa: BLE001 - a broken or hostile image is "undecodable", never a server error
        raise ThumbnailError("undecodable") from error
