"""CSI camera driver — thin CameraDriver wrapper around camera/camera.py's
existing, already-working Picamera2 logic.

Doesn't reimplement the actual capture: camera.py's init_camera() /
switch_camera_mode() / get_camera_frame() / _capture_still() / stop_camera()
are the real, already-production-proven Picamera2 integration running on
dev/prod today. This driver just adapts that module-level API to the
CameraDriver contract so camera_manager.py can dispatch to it exactly like
usb_driver.py.

One real behavioral difference from camera.py's own generate_mjpeg_stream():
that function builds the full multipart/x-mixed-replace envelope itself
(--frame\\r\\nContent-Type: image/jpeg\\r\\n...). CameraDriver.stream_mjpeg()
must yield only raw JPEG bytes per the shared contract (see
camera/camera_driver.py) - camera_manager.py owns the envelope for every
driver, the same way it will for usb_driver.py's native MJPEG passthrough.
This driver therefore re-runs the same frame-pacing loop
generate_mjpeg_stream() uses (get_camera_frame() -> PIL encode) but yields
only the encoded bytes, not the boundary/headers around them - not a second
capture implementation, just the envelope stripped out.

Similarly, capture_photo() calls camera.py's _capture_still() directly
(the same helper capture_photo_preview() already uses internally) instead
of going through capture_photo_preview(), which base64-encodes its result
for the JSON API response - CameraDriver.capture_photo() returns raw bytes,
so the base64 round-trip would be pure overhead here.
"""

from __future__ import annotations

import io
import re
import time
from typing import Any, Iterator

from camera import camera as camera_module
from camera.camera_driver import CameraDriver

# libcamera's uvcvideo pipeline handler always suffixes a USB camera's Id
# with "-vendor:product" (confirmed live on a Pi 4B+ with two UVC webcams:
# a Logitech C170's *Model* string is just "Webcam C170: Webcam C170" - no
# vendor:product anywhere - while a Logitech E3500's Model happens to be
# the generic uvcvideo fallback "UVC Camera (046d:09a4)", which does. Model
# formatting apparently depends on whether the camera has its own USB
# product-string descriptor, so it's not a reliable place to look for
# this - both cameras' Id ends in "-vendor:product" regardless. A real CSI
# sensor's Id is a platform device path (e.g. an i2c bus path) with no such
# suffix, so this correctly reports no USB ids for genuine CSI hardware.
_USB_ID_SUFFIX_RE = re.compile(r"-([0-9a-fA-F]{4}):([0-9a-fA-F]{4})$")


def _detect_usb_ids() -> tuple[str, str] | None:
    """USB vendor:product ids of the camera camera.py's Picamera2()
    actually opened (always index 0 of global_camera_info()), if it's a
    USB webcam seen through uvcvideo rather than a real CSI sensor."""
    try:
        from picamera2 import Picamera2

        info = Picamera2.global_camera_info()
        if not info:
            return None
        match = _USB_ID_SUFFIX_RE.search(str(info[0].get("Id") or ""))
        if not match:
            return None
        return match.group(1).lower(), match.group(2).lower()
    except Exception:
        return None


class CsiCameraDriver(CameraDriver):
    id = "csi"
    display_name = "CSI Camera"

    def detect(self) -> dict[str, Any] | None:
        if not camera_module.CAMERA_AVAILABLE:
            if not camera_module.init_camera():
                return None
        model = str(getattr(camera_module, "CAMERA_MODEL", "") or "").strip()
        return {"model": model or "CSI Camera", "usb_ids": _detect_usb_ids()}

    def start(self, resolution: str | None = None, fps: int | None = None, **_options: Any) -> bool:
        if not camera_module.CAMERA_AVAILABLE:
            if not camera_module.init_camera():
                return False

        video_config = camera_module.VIDEO_CONFIG
        target_resolution = resolution or video_config.get("resolution")
        target_fps = fps or video_config.get("fps")
        return camera_module.switch_camera_mode("video", resolution=target_resolution, fps=target_fps)

    def stop(self) -> None:
        camera_module.stop_camera()

    def get_status(self) -> dict[str, Any]:
        status = camera_module.get_camera_status()
        return {
            "ok": bool(status.get("ok")),
            "started": bool(status.get("started")),
            "model": str(getattr(camera_module, "CAMERA_MODEL", "") or ""),
            "resolution": status.get("resolution"),
            "fps": status.get("fps"),
        }

    def stream_mjpeg(self) -> Iterator[bytes]:
        if not camera_module.start_camera():
            print("[CSI CAMERA] Cannot start for streaming", flush=True)
            return

        # Same generation-tracking pattern as generate_mjpeg_stream() -
        # if something else in camera.py (mode switch, power off) bumps
        # stream_generation, this loop notices and exits instead of
        # yielding frames from a camera that just got reconfigured.
        generation = camera_module.stream_generation
        video_config = camera_module.VIDEO_CONFIG
        frame_interval = 1.0 / max(1, int(video_config.get("fps", 12)))
        quality = int(video_config.get("quality", 75))
        last_send_time = 0.0

        while generation == camera_module.stream_generation:
            now = time.time()
            if now - last_send_time < frame_interval:
                time.sleep(0.01)
                continue

            frame = camera_module.get_camera_frame()
            if generation != camera_module.stream_generation:
                break
            if frame is None:
                time.sleep(0.05)
                continue

            from PIL import Image

            img = Image.fromarray(frame)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            yield buf.getvalue()
            last_send_time = now

    def capture_photo(self, resolution: str | None = None) -> bytes:
        photo_config = camera_module.PHOTO_CONFIG
        target_resolution = resolution or photo_config.get("resolution", "640x480")
        quality = int(photo_config.get("quality", 85))

        try:
            img, _width, _height = camera_module._capture_still(
                target_resolution, quality, "[CSI CAMERA]"
            )
        except Exception as error:
            print(f"[CSI CAMERA] Photo capture error: {error}", flush=True)
            return b""

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()

    def list_resolutions(self) -> list[str]:
        return list(getattr(camera_module, "RESOLUTIONS", []) or [])
