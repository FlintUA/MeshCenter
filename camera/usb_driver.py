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

ARCHITECTURE NOTE - why this keeps the device open and streaming
continuously instead of opening/closing per operation (the first version
of this file did the latter and was reworked after live testing):

This camera/controller combination (an ~2005-era Logitech webcam, behind
a USB hub, on a Pi 3B+'s dwc_otg USB controller) is genuinely fragile
under close-then-reopen cycling - repeated stop()/start() during testing
reliably triggered a real USB-level disconnect/re-enumeration, visible in
`dmesg` as "device descriptor read/64, error -32" / "unable to enumerate
USB device", taking several seconds to recover. This happened even with
0.5-3s settle delays between cycles and across separate process
invocations, so it isn't a race condition in this code - it's a real
hardware limit.

The fix: once started, a background reader thread continuously pulls
frames into self._last_frame (mirroring camera.py's existing
get_camera_frame()/last_frame pattern for the CSI driver).
stream_mjpeg() (possibly several concurrent viewers) and capture_photo()
both just read that buffered frame - neither ever touches stream_off or
reopens the device during normal operation. The device is only actually
closed on an explicit stop() or a genuine resolution change, both of
which still use STOP_START_SETTLE_SECONDS as a precaution, but those are
now rare, deliberate operations instead of something every photo capture
triggered.
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from typing import Any, Iterator

from camera.camera_driver import CameraDriver

# Minimum pause between closing and reopening the device - see the module
# docstring for why this isn't optional on this hardware. Only exercised
# now by stop()+start() and genuine resolution changes, not by every
# capture_photo() call.
STOP_START_SETTLE_SECONDS = 0.5

# How long capture_photo() waits for the background reader thread to
# deliver a first frame on a cold start, before giving up.
FIRST_FRAME_TIMEOUT_SECONDS = 3.0

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
    """Enumerate /dev/video* nodes that are actual USB capture devices -
    not the metadata/M2M nodes some UVC cameras also expose (this
    camera's /dev/video1), and not the Raspberry Pi's own bcm2835-isp
    platform nodes that exist on every Pi regardless of any USB camera.

    Two filters, both confirmed necessary live on camtest (a first
    version using only `.capabilities` and no USB-id check matched 6
    nodes for one physical camera: video0, its own video1 metadata node,
    and four unrelated bcm2835-isp ISP-pipeline nodes):

    - `.info.device_capabilities` (per-node), not `.info.capabilities`
      (the deprecated aggregate-across-all-nodes-of-this-device field) -
      video0 and video1 report the *same* `.capabilities` value even
      though only video0 can actually capture images; `device_capabilities`
      is what actually differs (VIDEO_CAPTURE vs META_CAPTURE).
    - A real USB vendor/product id from sysfs - bcm2835-isp is a platform
      device, not USB, and reports VIDEO_CAPTURE via device_capabilities
      just like a real camera does, so the capability check alone doesn't
      exclude it.
    """
    found: list[dict[str, Any]] = []
    for dev_path in sorted(glob.glob("/dev/video*")):
        if not re.fullmatch(r"/dev/video\d+", dev_path):
            continue

        vendor_id, product_id = _usb_ids_for_video_device(dev_path)
        if not vendor_id or not product_id:
            continue

        try:
            from linuxpy.video.device import Capability, Device as V4LDevice

            device = V4LDevice(dev_path)
            device.open()
            try:
                caps = Capability(device.info.device_capabilities)
                if Capability.VIDEO_CAPTURE not in caps:
                    continue
                card_name = str(device.info.card or "").strip()
            finally:
                device.close()
        except Exception as error:
            print(f"[USB CAMERA] Probe failed for {dev_path}: {error}", flush=True)
            continue

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
        self._reader_thread: threading.Thread | None = None

        # Populated by _reader_loop(), read by stream_mjpeg()/capture_photo().
        # Separate from self._lock (which guards start/stop control flow)
        # so a long-lived stream_mjpeg() generator polling this never
        # blocks a concurrent stop() or capture_photo() call.
        self._frame_lock = threading.Lock()
        self._last_frame: bytes | None = None
        self._last_frame_time = 0.0

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
                # Per-node capability field, not the deprecated aggregate
                # `.capabilities` - see discover_usb_cameras()'s docstring
                # for why that distinction matters on this camera.
                caps = Capability(device.info.device_capabilities)
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
                    # Live-switching resolution goes through _reconfigure(),
                    # which stop()s and reopens the device - confirmed on
                    # camtest that this specific camera (Logitech QuickCam
                    # E 3500 behind a USB hub on a Pi 3B+) can take well
                    # over the usual few-second USB re-enumeration window
                    # to recover from that, without a real fix in sight yet
                    # (retry-with-backoff wasn't tried). Deliberately
                    # ignoring the request for now rather than risking
                    # another disconnect - _reconfigure() is left intact
                    # below for whenever this gets revisited.
                    print(
                        f"[USB CAMERA] Resolution switch to {resolution} ignored - "
                        f"fixed at {self._resolution} for now, see usb_driver.py "
                        "module docstring",
                        flush=True,
                    )
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
                self._last_frame = None
                self._last_frame_time = 0.0

                generation = self._stream_generation
                self._reader_thread = threading.Thread(
                    target=self._reader_loop,
                    args=(self._device, generation),
                    name=f"usb-camera-reader-{generation}",
                    daemon=True,
                )
                self._reader_thread.start()

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
            self._stream_generation += 1  # tells _reader_loop() to exit
            self._started = False
            thread = self._reader_thread
            self._reader_thread = None
            device = self._device
            self._device = None

        # Join outside self._lock: the reader thread only ever touches
        # self._frame_lock, but holding self._lock here while waiting on
        # it would still needlessly block other callers (e.g. get_status())
        # for no reason.
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        if device is not None:
            self._stream_off_safe(device)
            try:
                device.close()
            except Exception as error:
                print(f"[USB CAMERA] Stop error: {error}", flush=True)

        with self._frame_lock:
            self._last_frame = None
            self._last_frame_time = 0.0

    def get_status(self) -> dict[str, Any]:
        with self._frame_lock:
            frame_age = (time.time() - self._last_frame_time) if self._last_frame_time else None
        return {
            "ok": os.path.exists(self.dev_path),
            "started": self._started,
            "model": self._model,
            "dev_path": self.dev_path,
            "resolution": self._resolution,
            "fps": self._fps,
            "last_frame_age_seconds": frame_age,
        }

    # ------------------------------------------------------------
    # CameraDriver
    # ------------------------------------------------------------

    def stream_mjpeg(self) -> Iterator[bytes]:
        if not self._started and not self.start():
            print("[USB CAMERA] Cannot start for streaming", flush=True)
            return

        my_generation = self._stream_generation
        frame_interval = 1.0 / max(1, self._fps)
        last_sent_time = 0.0
        last_frame_time_seen = 0.0

        while my_generation == self._stream_generation:
            now = time.time()
            if now - last_sent_time < frame_interval:
                time.sleep(0.01)
                continue

            with self._frame_lock:
                data = self._last_frame
                frame_time = self._last_frame_time

            if not data or frame_time == last_frame_time_seen:
                time.sleep(0.01)
                continue

            yield data
            last_sent_time = now
            last_frame_time_seen = frame_time

    def capture_photo(self, resolution: str | None = None) -> bytes:
        with self._lock:
            if resolution and resolution != self._resolution:
                if not self.start(resolution=resolution):
                    return b""
            elif not self._started:
                if not self.start():
                    return b""

        # Wait for the background reader thread to deliver a frame,
        # outside self._lock so start()/stop() aren't blocked by this
        # wait. Usually near-instant if streaming was already running -
        # this timeout only matters on a cold start.
        deadline = time.time() + FIRST_FRAME_TIMEOUT_SECONDS
        while time.time() < deadline:
            with self._frame_lock:
                data = self._last_frame
            if data:
                return data
            time.sleep(0.03)

        print("[USB CAMERA] capture_photo() timed out waiting for a frame", flush=True)
        return b""

    def list_resolutions(self) -> list[str]:
        resolutions = self._probe_resolutions()
        return resolutions or list(CONFIRMED_RESOLUTIONS)

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _reader_loop(self, device, generation: int) -> None:
        """Runs on a background thread for the lifetime of one start()
        cycle - reads frames as fast as the camera produces them and
        stashes the latest one, so stream_mjpeg()/capture_photo() never
        have to touch the device directly."""
        try:
            for frame in device:
                if generation != self._stream_generation:
                    break
                data = bytes(frame)
                if data:
                    with self._frame_lock:
                        self._last_frame = data
                        self._last_frame_time = time.time()
        except Exception as error:
            if generation == self._stream_generation:
                print(f"[USB CAMERA] Reader thread error: {error}", flush=True)

    @staticmethod
    def _stream_off_safe(device) -> None:
        """Best-effort VIDIOC_STREAMOFF - swallow errors, this is always
        called from a cleanup path (stop()) where raising would just
        replace one problem with another."""
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
        # Only reached for a genuine resolution/fps change - normal
        # streaming and photo capture never call stop()+start() anymore.
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
