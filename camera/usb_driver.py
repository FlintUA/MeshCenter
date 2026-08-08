"""USB/UVC camera driver — raw V4L2 via v4l2py, MJPEG passthrough.

Confirmed hardware (camtest, Raspberry Pi 3B+, 2026-08-08): Logitech
QuickCam E 3500 (USB id 046d:09a4) at /dev/video0, native MJPEG,
160x120 up to 640x480, up to 30fps.

No decode/re-encode happens here: the camera already produces MJPEG in
hardware, so each frame v4l2py hands back is forwarded to
CameraDriver.stream_mjpeg() unmodified. This is the "MJPEG passthrough"
half of the plan - contrast with csi_driver.py, which has to decode a raw
sensor frame and re-encode it via PIL because Picamera2 has no native
MJPEG capture path.

NOTE: written without live access to the actual hardware (SSH to camtest
wasn't set up yet as of this writing) - the v4l2py call shapes below are
believed correct but unverified against the installed v4l2py version.
First real run on camtest is the actual test; watch [USB CAMERA] log
lines for anything that doesn't match.
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from typing import Any, Iterator

from camera.camera_driver import CameraDriver

# Frame sizes this exact camera (Logitech QuickCam E 3500) was confirmed to
# support during live testing on camtest. Used as a fallback if runtime
# enumeration via v4l2py (_probe_resolutions) fails for any reason - this
# list is a known-good floor, not a guess.
CONFIRMED_RESOLUTIONS = ["160x120", "176x144", "320x240", "352x288", "640x480"]

DEFAULT_RESOLUTION = "640x480"
DEFAULT_FPS = 30


def _read_sys_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _usb_ids_for_video_device(dev_path: str) -> tuple[str, str]:
    """Return (vendor_id, product_id) hex strings for a /dev/videoN node,
    read from sysfs - independent of v4l2py, so it works even if the
    format-negotiation part of this driver has trouble."""
    name = os.path.basename(dev_path)
    sys_device_dir = f"/sys/class/video4linux/{name}/device"
    real = os.path.realpath(sys_device_dir)
    # Walk up from the video4linux child node to the actual USB device
    # directory, which is where idVendor/idProduct live.
    probe = real
    for _ in range(6):
        vendor = _read_sys_text(os.path.join(probe, "idVendor"))
        product = _read_sys_text(os.path.join(probe, "idProduct"))
        if vendor and product:
            return vendor, product
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return "", ""


def discover_usb_cameras() -> list[dict[str, Any]]:
    """Enumerate /dev/video* nodes that are actual capture devices (not
    the metadata/M2M nodes some UVC cameras also expose), independent of
    v4l2py so discovery works even before a driver instance is created.
    """
    found: list[dict[str, Any]] = []
    for dev_path in sorted(glob.glob("/dev/video*")):
        match = re.fullmatch(r"/dev/video(\d+)", dev_path)
        if not match:
            continue
        try:
            from v4l2py import Device as V4L2Device

            with V4L2Device(dev_path) as probe_device:
                caps = probe_device.info.capabilities
                # v4l2py exposes capabilities as a flag-enum-like object;
                # comparing by name keeps this independent of the exact
                # enum implementation across versions.
                if "VIDEO_CAPTURE" not in str(caps):
                    continue
                card_name = str(getattr(probe_device.info, "card", "") or "").strip()
        except Exception as error:
            print(f"[USB CAMERA] Probe failed for {dev_path}: {error}", flush=True)
            continue

        vendor_id, product_id = _usb_ids_for_video_device(dev_path)
        found.append({
            "dev_path": dev_path,
            "card": card_name,
            "vendor_id": vendor_id,
            "product_id": product_id,
        })
    return found


class UsbCameraDriver(CameraDriver):
    def __init__(self, dev_path: str = "/dev/video0"):
        self.dev_path = dev_path
        vendor_id, product_id = _usb_ids_for_video_device(dev_path)
        dev_name = os.path.basename(dev_path)
        self.id = f"usb:{vendor_id or 'unknown'}:{product_id or 'unknown'}:{dev_name}"
        self.display_name = "USB Camera"

        self._lock = threading.RLock()
        self._device = None  # v4l2py Device, opened lazily in start()
        self._started = False
        self._model = ""
        self._resolution = DEFAULT_RESOLUTION
        self._fps = DEFAULT_FPS
        self._stream_generation = 0

    # ------------------------------------------------------------
    # DeviceDriver
    # ------------------------------------------------------------

    def detect(self) -> dict[str, Any] | None:
        try:
            from v4l2py import Device as V4L2Device
        except ImportError as error:
            print(f"[USB CAMERA] v4l2py not installed: {error}", flush=True)
            return None

        if not os.path.exists(self.dev_path):
            return None

        try:
            with V4L2Device(self.dev_path) as probe_device:
                card_name = str(getattr(probe_device.info, "card", "") or "").strip()
        except Exception as error:
            print(f"[USB CAMERA] Detect failed for {self.dev_path}: {error}", flush=True)
            return None

        self._model = card_name or "USB Camera"
        vendor_id, product_id = _usb_ids_for_video_device(self.dev_path)
        return {
            "model": self._model,
            "vendor_id": vendor_id,
            "product_id": product_id,
            "dev_path": self.dev_path,
        }

    def start(self, resolution: str | None = None, fps: int | None = None, **_options: Any) -> bool:
        with self._lock:
            if self._started and self._device is not None:
                if resolution and resolution != self._resolution:
                    return self._reconfigure(resolution, fps or self._fps)
                return True

            try:
                from v4l2py import Device as V4L2Device
            except ImportError as error:
                print(f"[USB CAMERA] v4l2py not installed: {error}", flush=True)
                return False

            try:
                self._device = V4L2Device(self.dev_path)
                self._device.open()
                self._model = str(getattr(self._device.info, "card", "") or "").strip() or self._model

                target_resolution = resolution or self._resolution
                target_fps = fps or self._fps
                if not self._apply_format(target_resolution, target_fps):
                    self._device.close()
                    self._device = None
                    return False

                self._resolution = target_resolution
                self._fps = target_fps
                self._started = True
                self._stream_generation += 1
                print(
                    f"[USB CAMERA] Started {self.dev_path} ({self._model}) "
                    f"at {self._resolution} MJPEG",
                    flush=True,
                )
                return True
            except Exception as error:
                print(f"[USB CAMERA] Start failed: {error}", flush=True)
                self._started = False
                if self._device is not None:
                    try:
                        self._device.close()
                    except Exception:
                        pass
                self._device = None
                return False

    def stop(self) -> None:
        with self._lock:
            if self._device is not None:
                try:
                    self._device.close()
                except Exception as error:
                    print(f"[USB CAMERA] Stop error: {error}", flush=True)
            self._device = None
            self._started = False
            self._stream_generation += 1

    def get_status(self) -> dict[str, Any]:
        return {
            "ok": os.path.exists(self.dev_path),
            "started": self._started,
            "model": self._model,
            "dev_path": self.dev_path,
            "resolution": self._resolution,
            "fps": self._fps,
        }

    # ------------------------------------------------------------
    # CameraDriver
    # ------------------------------------------------------------

    def stream_mjpeg(self) -> Iterator[bytes]:
        if not self._started and not self.start():
            print("[USB CAMERA] Cannot start for streaming", flush=True)
            return

        my_generation = self._stream_generation
        device = self._device
        if device is None:
            return

        try:
            for frame in device:
                if my_generation != self._stream_generation:
                    # stop()/reconfigure happened underneath this generator -
                    # exit quietly instead of yielding frames from a
                    # closed/reconfigured device.
                    return
                data = bytes(frame)
                if data:
                    yield data
        except Exception as error:
            print(f"[USB CAMERA] Stream error: {error}", flush=True)

    def capture_photo(self, resolution: str | None = None) -> bytes:
        with self._lock:
            was_started = self._started
            previous_resolution = self._resolution

            if resolution and resolution != self._resolution:
                if not self.start(resolution=resolution):
                    return b""
            elif not self._started:
                if not self.start():
                    return b""

            device = self._device
            if device is None:
                return b""

            try:
                for frame in device:
                    data = bytes(frame)
                    if data:
                        photo = data
                        break
                else:
                    photo = b""
            except Exception as error:
                print(f"[USB CAMERA] Photo capture error: {error}", flush=True)
                photo = b""

            # Restore the streaming resolution if we temporarily switched
            # for a higher/lower-res still capture.
            if resolution and resolution != previous_resolution and was_started:
                self.start(resolution=previous_resolution)

            return photo

    def list_resolutions(self) -> list[str]:
        resolutions = self._probe_resolutions()
        return resolutions or list(CONFIRMED_RESOLUTIONS)

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _apply_format(self, resolution: str, fps: int) -> bool:
        try:
            width, height = (int(part) for part in resolution.split("x"))
        except (TypeError, ValueError):
            print(f"[USB CAMERA] Invalid resolution string: {resolution!r}", flush=True)
            return False

        device = self._device
        if device is None:
            return False

        try:
            # v4l2py's format setter is exposed on the device's
            # video_capture sub-object in the versions this was written
            # against; fall back to a direct set_format() on the device
            # itself if that shape differs in the installed version.
            video_capture = getattr(device, "video_capture", None)
            if video_capture is not None and hasattr(video_capture, "set_format"):
                video_capture.set_format(width, height, "MJPG")
            elif hasattr(device, "set_format"):
                device.set_format(width, height, "MJPG")
            else:
                print(
                    "[USB CAMERA] No known set_format() entry point on this "
                    "v4l2py Device - API shape mismatch, needs checking "
                    "against the installed v4l2py version",
                    flush=True,
                )
                return False

            try:
                if hasattr(device, "set_fps"):
                    device.set_fps(fps)
                elif video_capture is not None and hasattr(video_capture, "set_fps"):
                    video_capture.set_fps(fps)
            except Exception as fps_error:
                # Frame rate is a nice-to-have; MJPEG capture at the
                # camera's default fps for this resolution still works.
                print(f"[USB CAMERA] set_fps() failed, continuing without it: {fps_error}", flush=True)

            return True
        except Exception as error:
            print(f"[USB CAMERA] Format negotiation failed ({width}x{height} MJPG): {error}", flush=True)
            return False

    def _reconfigure(self, resolution: str, fps: int) -> bool:
        self.stop()
        return self.start(resolution=resolution, fps=fps)

    def _probe_resolutions(self) -> list[str]:
        try:
            from v4l2py import Device as V4L2Device
        except ImportError:
            return []

        try:
            with V4L2Device(self.dev_path) as probe_device:
                video_capture = getattr(probe_device, "video_capture", None)
                formats = getattr(video_capture, "formats", None) if video_capture else None
                if not formats:
                    return []

                sizes: set[str] = set()
                for fmt in formats:
                    pixel_format = str(getattr(fmt, "pixel_format", ""))
                    if "MJPG" not in pixel_format.upper() and "MJPEG" not in pixel_format.upper():
                        continue
                    for size in getattr(fmt, "sizes", []) or getattr(fmt, "size", []):
                        width = getattr(size, "width", None)
                        height = getattr(size, "height", None)
                        if width and height:
                            sizes.add(f"{width}x{height}")
                return sorted(sizes, key=lambda s: int(s.split("x")[0]))
        except Exception as error:
            print(f"[USB CAMERA] Resolution probe failed, using confirmed fallback list: {error}", flush=True)
            return []
