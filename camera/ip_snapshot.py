"""A picture from a network camera into the Media gallery (CAM2 Phase 3a).

Two ways to get the picture, and neither opens a second RTSP session or touches the running preview:

1. **The camera's own snapshot API** (Reolink control, camera/reolink_control.py): a separate HTTP request,
   full resolution by default (the source's `snapshot_quality` can ask for the small sub picture).
2. **One frame from the running stream** (camera/frame_grab.py): the init segment and the newest keyframe are
   copied from the existing relay into a one-shot ffmpeg. Used for a source without the API, and as the
   fallback when the API cannot be reached while the preview runs. Only while the preview runs: otherwise
   `preview_not_running` ("start the preview first").

Per source at most one picture per RATE_LIMIT_S seconds. The gallery's own limits apply (camera.cleanup_old_screenshots:
500 MB / 30 days) and a full disk is a clear `storage_full` error, never a partial file: the picture is written to a
temporary name and renamed, its metadata (source, time, method, profile, size) goes to a sidecar `<name>.jpg.json`
that the gallery removes together with the picture.
"""
from __future__ import annotations

import errno
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime
from typing import Any, Callable

from camera import frame_grab
from camera import reolink_client as rc

RATE_LIMIT_S = 2.0
MIN_FREE_BYTES = 50 * 1024 * 1024
GALLERY_MAX_MB = 500
GALLERY_KEEP_DAYS = 30
# API failures after which the running preview can still deliver the picture; anything else (a wrong password, a
# missing right, a certificate to confirm) is reported, because the user has to fix it
FALLBACK_CODES = frozenset({rc.UNREACHABLE, rc.TIMEOUT, rc.API_ERROR, rc.BAD_RESPONSE, rc.TOO_MANY_SESSIONS, rc.REDIRECT_REFUSED})

_STATUS = {
    "rate_limited": 429,
    "preview_not_running": 409,
    "ffmpeg_missing": 503,
    "frame_grab_failed": 502,
    "storage_full": 507,
    "camera_off": 409,
}
_CONTROL_STATUS = {
    rc.NOT_SUPPORTED: 404, rc.LOGIN_FAILED: 401, rc.TOO_MANY_SESSIONS: 503, rc.PERMISSION_DENIED: 403,
    rc.HTTP_NOT_ALLOWED: 409, rc.ADDRESS_BLOCKED: 400, rc.TLS_UNTRUSTED: 409, rc.TLS_PIN_MISMATCH: 409,
}


class SnapshotError(Exception):
    def __init__(self, code: str, message: str, *, status: int | None = None, **extra: Any):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra
        self.status = status or _STATUS.get(code, 502)

    def to_body(self) -> dict:
        return {"ok": False, "error": self.message, "error_code": self.code, **self.extra}


# Russian and Ukrainian letters to ASCII (a simple, language-neutral scheme; the real name stays in the sidecar metadata).
_CYRILLIC = {
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya", "ʼ": "", "'": "", "’": "",
}


def transliterate_cyrillic(text: str) -> str:
    """Russian and Ukrainian letters to ASCII ("Гараж" -> "Garazh"); every other character is left as it is. A capital
    letter gives a capitalised result ("Щ" -> "Shch"), an all-capital word stays readable ("ГАРАЖ" -> "GARAZH")."""
    out = []
    chars = str(text or "")
    for index, ch in enumerate(chars):
        mapped = _CYRILLIC.get(ch.lower())
        if mapped is None:
            out.append(ch)
        elif ch.islower() or not mapped:
            out.append(mapped)
        else:
            neighbour_upper = (index + 1 < len(chars) and chars[index + 1].isupper()) or (index > 0 and chars[index - 1].isupper())
            out.append(mapped.upper() if neighbour_upper else mapped.capitalize())
    return "".join(out)


def slug(name: str, source_id: str | None = None) -> str:
    """ASCII, filesystem-safe piece of the source name for the file name; the real name is in the metadata. Cyrillic is
    transliterated first. A name with nothing left after that (an emoji, Chinese, ...) falls back to
    `camera-<short stable id>` from the source id, so two such cameras never share a prefix; without a source id it is
    plain `camera`."""
    text = re.sub(r"[^A-Za-z0-9]+", "-", transliterate_cyrillic(str(name or ""))).strip("-")[:24].strip("-")
    if text:
        return text
    short = re.sub(r"[^a-z0-9]", "", str(source_id or "").lower().replace("ip:", "", 1))[:6]
    return "camera-" + short if short else "camera"


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) from the first start-of-frame marker, or None."""
    i, n = 2, len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        if marker == 0xFF:
            i += 1
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


class IpSnapshotService:
    def __init__(self, store, control, camera_module, *, clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], datetime] = datetime.now, grabber: Callable[..., bytes] | None = None,
                 free_bytes: Callable[[str], int] | None = None):
        self._store, self._control, self._camera = store, control, camera_module
        self._clock, self._wall = clock, wall
        self._grab = grabber or frame_grab.grab_frame
        self._free = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    # -- the one entry point -------------------------------------------------

    def take(self, driver) -> dict:
        """Take, store and describe one picture of the active network camera. Raises SnapshotError."""
        source_id = driver.id
        self._rate_limit(source_id)
        source = self._store.get_source(source_id)
        control = source.get("control") or {}
        jpeg, method, fallback_reason = self._picture(driver, source_id, control)
        return self._save(source, control, jpeg, method, fallback_reason)

    def _rate_limit(self, source_id: str) -> None:
        with self._lock:
            now = self._clock()
            last = self._last.get(source_id)
            if last is not None and now - last < RATE_LIMIT_S:
                raise SnapshotError("rate_limited", "Pictures are limited to one every two seconds.",
                                    retry_after_s=round(RATE_LIMIT_S - (now - last), 1))
            self._last[source_id] = now

    # -- getting the picture ---------------------------------------------------

    def _picture(self, driver, source_id: str, control: dict) -> tuple[bytes, str, str]:
        """(jpeg, method, fallback_reason). A fallback_reason means the camera's own picture was wanted and could not
        be had: the user is told, never silently handed the (smaller) stream frame."""
        reason = ""
        if control.get("type") == "reolink":
            try:
                small = control.get("snapshot_quality") == "small"
                return self._control.capture(source_id, small=small), "camera_api_small" if small else "camera_api_full", ""
            except rc.ReolinkError as error:
                if error.code not in FALLBACK_CODES or driver.upstream.relay.snapshot_input() is None:
                    raise self._from_control(error)
                reason = error.code  # the API is unreachable but the preview runs: the picture comes from the running stream
        try:
            return self._grab(driver.upstream.relay), "stream_frame", reason
        except frame_grab.GrabError as error:
            raise SnapshotError(error.code, error.message)

    @staticmethod
    def _from_control(error: rc.ReolinkError) -> SnapshotError:
        extra = {k: v for k, v in error.to_dict().items() if k in ("command", "right", "fingerprint")}
        return SnapshotError(error.code, error.message, status=_CONTROL_STATUS.get(error.code, 502), **extra)

    # -- storing it --------------------------------------------------------------

    def _save(self, source: dict, control: dict, jpeg: bytes, method: str, fallback_reason: str = "") -> dict:
        cam = self._camera
        os.makedirs(cam.SCREENSHOTS_DIR, exist_ok=True)
        cam.cleanup_old_screenshots(max_mb=GALLERY_MAX_MB, keep_days=GALLERY_KEEP_DAYS)
        if self._free(cam.SCREENSHOTS_DIR) < max(MIN_FREE_BYTES, 3 * len(jpeg)):
            raise SnapshotError("storage_full", "The gallery storage is full. Delete pictures or free space on the disk.")
        taken = self._wall()
        directory = cam.get_screenshot_day_dir(taken)
        filename = cam.make_screenshot_filename(taken, prefix="MC_" + slug(source.get("name"), source.get("id")))
        path = os.path.join(directory, filename)
        size = jpeg_size(jpeg)
        profile = {"camera_api_full": "full", "camera_api_small": "small"}.get(method, str(source.get("preferred_profile") or ""))
        meta = {"source_id": source["id"], "source_name": source.get("name") or "", "taken_at": taken.isoformat(timespec="seconds"),
                "method": method, "profile": profile}
        if fallback_reason:
            meta["fallback_reason"] = fallback_reason
        if size:
            meta["width"], meta["height"] = size
        part, side_part = path + ".part", cam.sidecar_path(path) + ".part"
        try:
            with open(part, "wb") as handle:
                handle.write(jpeg)
            with open(side_part, "w", encoding="utf-8") as handle:
                json.dump(meta, handle)
            os.replace(side_part, cam.sidecar_path(path))
            os.replace(part, path)  # the picture appears last, with its metadata already in place
        except OSError as error:
            for leftover in (part, side_part, cam.sidecar_path(path)):
                try:
                    os.remove(leftover)
                except OSError:
                    pass
            if error.errno in (errno.ENOSPC, errno.EDQUOT):
                raise SnapshotError("storage_full", "The gallery storage is full. Delete pictures or free space on the disk.")
            raise SnapshotError("storage_write_failed", "The picture could not be saved.", status=500)
        rel = os.path.relpath(path, cam.SCREENSHOTS_DIR).replace("\\", "/")
        return {"ok": True, "success": True, "filename": rel, "display_name": filename, "size": len(jpeg),
                "source": meta["source_name"], "method": method, "profile": profile,
                "resolution": f"{size[0]}x{size[1]}" if size else "",
                "requested_quality": (control.get("snapshot_quality") or "full") if control.get("type") == "reolink" else "",
                "fallback": bool(fallback_reason), "fallback_reason": fallback_reason}
