"""USB/UVC camera driver — raw V4L2 via linuxpy, MJPEG passthrough.

Confirmed hardware (camtest, Raspberry Pi 3B+, 192.168.2.107,
2026-08-08): Logitech QuickCam E 3500, USB id 046d:09a4, at /dev/video0
(/dev/video1 is its paired metadata node, not a capture device). Native
MJPEG at 160x120, 176x144, 320x240, 352x288, 640x480 - verified live via
Device.info.frame_sizes().

No decode/re-encode happens here: the camera already produces MJPEG in
hardware, so each frame linuxpy hands back (confirmed to start with the
JPEG SOI marker 0xFFD8) is forwarded to CameraDriver.stream_mjpeg()
unmodified. This is the "MJPEG passthrough" half of the plan - contrast
with csi_driver.py, which has to decode a raw sensor frame and re-encode
it via PIL because Picamera2 has no native MJPEG capture path.

Uses `linuxpy.video.device` directly, not the `v4l2py` package - v4l2py
3.0 is just a deprecated compatibility shim ("v4l2py is no longer being
maintained, please consider using linuxpy.video instead", printed as a
UserWarning on import) that re-exports linuxpy's own Device class.
requirements.txt pulls in linuxpy transitively via v4l2py>=3.0.0; importing
linuxpy.video.device directly here avoids the deprecation warning noise on
every server start.

Every call shape below (set_format's BufferType argument, get_fps/set_fps,
Capability bitmask checking, frame_sizes() enumeration) was verified live
against the actual installed linuxpy 0.24.0 on camtest, not assumed from
memory - see the session notes for the exact probe commands run.

Also observed live: this camera/controller combination (an ~2005-era
Logitech webcam on a Pi 3B+'s dwc_otg USB controller) is genuinely fragile
under rapid close-then-reopen cycling - a quick sequence of stop()/start()
calls during testing triggered a real USB-level disconnect/re-enumeration
(visible in `dmesg` as "device descriptor read/64, error -32" followed by
"unable to enumerate USB device" before it recovered a few seconds later).
STOP_START_SETTLE_SECONDS exists because of that, not as a defensive
guess.
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from typing import Any, Iterator

from camera.camera_driver import CameraDriver

# Minimum pause between closing and reopening the device (stop() followed
# by start()) - see the module docstring for why this isn't optional on
# this hardware.
STOP_START_SETTLE_SECONDS = 0.5

# Frame sizes this exact camera (Logitech QuickCam E 3500) was confirmed to
# support live via Device.info.frame_sizes() on camtest. Used as a fallback
# if runtime enumeration fails for any reason - this list is a known-good
# floor, not a guess.
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
    read from sysfs - independent of linuxpy, so it works even if the
    format-negotiation part of this driver has trouble.

    /sys/class/video4linux/videoN/device is a symlink to the USB
    *interface* directory (e.g. .../1-1.1.2:1.0); idVendor/idProduct live
    one level up, on the actual USB device directory (.../1-1.1.2) -
    verified against the real camtest hardware path.
    """
    name = os.path.basename(dev_path)
    sys_device_dir = f"/sys/class/video4linux/{name}/device"
    real = os.path.realpath(sys_device_dir)
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
    the metadata/M2M nodes some UVC cameras also expose - e.g. this
    camera's /dev/video1), independent of any single driver instance.
    """
    found: list[dict[str, Any]] = []
    for dev_path in sorted(glob.glob("/dev/video*")):
        if not re.fullmatch(r"/dev/video\d+", dev_path):
            continue

        try:
            from linuxpy.video.device import Capability, Device as V4LDevice

            device = V4LDevice(dev_path)
            device.open()
            try:
                caps = Capability(device.info.capabilities)
                if Capability.VIDEO_CAPTURE not in caps:
                    continue
                card_name = str(device.info.card or "").strip()
            finally:
                device.close()
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
        self._device = None  # linuxpy Device, opened lazily in start()
        self._started = False
        self._model = ""
        self._resolution = DEFAULT_RESOLUTION
        self._fps = DEFAULT_FPS
        self._stream_generation = 0

    # ------------------------------------------------------------
    # DeviceDriver
    # ------------------------------------------------------------

    def detect(self) -> dict[str, Any] | None:
        if not os.path.exists(self.dev_path):
            return None

        try:
            from linuxpy.video.device import Capability, Device as V4LDevice

            device = V4LDevice(self.dev_path)
            device.open()
            try:
                caps = Capability(device.info.capabilities)
                if Capability.VIDEO_CAPTURE not in caps:
                    return None
                card_name = str(device.info.card or "").strip()
            finally:
                device.close()
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
                from linuxpy.video.device import Device as V4LDevice
            except ImportError as error:
                print(f"[USB CAMERA] linuxpy not installed: {error}", flush=True)
                return False

            try:
                self._device = V4LDevice(self.dev_path)
                self._device.open()
                self._model = str(self._device.info.card or "").strip() or self._model

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
                self._stream_off_safe(self._device)
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
        finally:
            # Confirmed live on camtest: breaking out of `for frame in
            # device` (client disconnect, generation change) without an
            # explicit stream_off() leaves the camera's own state machine
            # confused - the *next* open()/set_format() on this same
            # camera fails with "[Errno 16] Device or resource busy" for a
            # few seconds until it self-recovers. Always release the
            # stream here regardless of how this generator exits.
            if my_generation == self._stream_generation:
                self._stream_off_safe(device)

    def capture_photo(self, resolution: str | None = None) -> bytes:
        with self._lock:
            was_started = self._started
            previous_resolution = self._resolution
            target_resolution = resolution or self._resolution

            # Always capture from a freshly (re)started stream, mirroring
            # the CSI driver's existing video/photo mode split
            # (switch_camera_mode() always stops the camera first before
            # reconfiguring - the two capture kinds are mutually exclusive,
            # not concurrent, on that driver too). Confirmed live on
            # camtest that grabbing one frame from a device that was just
            # stream_off()'d - without a full stop()+start() to re-arm
            # streaming - fails with Errno 5, so this isn't just following
            # convention, it's the only sequence that actually works with
            # this camera/library combination.
            self.stop()
            time.sleep(STOP_START_SETTLE_SECONDS)
            if not self.start(resolution=target_resolution):
                return b""

            device = self._device
            photo = b""
            try:
                for frame in device:
                    data = bytes(frame)
                    if data:
                        photo = data
                    break
            except Exception as error:
                print(f"[USB CAMERA] Photo capture error: {error}", flush=True)
                photo = b""

            # A one-shot capture has no business leaving the stream
            # running - stop it, then restart continuous streaming only if
            # it was actually active before this call.
            self.stop()
            if was_started:
                time.sleep(STOP_START_SETTLE_SECONDS)
                self.start(resolution=previous_resolution)

            return photo

    def list_resolutions(self) -> list[str]:
        resolutions = self._probe_resolutions()
        return resolutions or list(CONFIRMED_RESOLUTIONS)

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    @staticmethod
    def _stream_off_safe(device) -> None:
        """Best-effort VIDIOC_STREAMOFF - swallow errors, this is always
        called from a cleanup path (finally blocks, stop()) where raising
        would just replace one problem with another. Confirmed live on
        camtest to be safe to call even when the device was never
        actually streaming."""
        if device is None:
            return
        try:
            from linuxpy.video.device import BufferType

            device.stream_off(BufferType.VIDEO_CAPTURE)
        except Exception as error:
            print(f"[USB CAMERA] stream_off() warning: {error}", flush=True)

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
            from linuxpy.video.device import BufferType

            device.set_format(BufferType.VIDEO_CAPTURE, width, height, "MJPG")
            try:
                device.set_fps(BufferType.VIDEO_CAPTURE, fps)
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
        time.sleep(STOP_START_SETTLE_SECONDS)
        return self.start(resolution=resolution, fps=fps)

    def _probe_resolutions(self) -> list[str]:
        try:
            from linuxpy.video.device import Device as V4LDevice, PixelFormat
        except ImportError:
            return []

        try:
            device = V4LDevice(self.dev_path)
            device.open()
            try:
                sizes: set[str] = set()
                for frame_size in device.info.frame_sizes():
                    if frame_size.pixel_format != PixelFormat.MJPEG:
                        continue
                    info = frame_size.info
                    width = getattr(info, "width", None)
                    height = getattr(info, "height", None)
                    if width and height:
                        sizes.add(f"{width}x{height}")
                return sorted(sizes, key=lambda s: int(s.split("x")[0]))
            finally:
                device.close()
        except Exception as error:
            print(f"[USB CAMERA] Resolution probe failed, using confirmed fallback list: {error}", flush=True)
            return []
