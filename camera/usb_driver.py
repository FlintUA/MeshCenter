"""USB/UVC camera driver — raw V4L2 via linuxpy, MJPEG passthrough with a
YUYV software-encode fallback for cameras that don't offer MJPEG at all.

Confirmed hardware:
- camtest, Raspberry Pi 3B+, 192.168.2.107, 2026-08-08: Logitech QuickCam
  E 3500, USB id 046d:09a4, at /dev/video0 (/dev/video1 is its paired
  metadata node, not a capture device). Native MJPEG at 160x120, 176x144,
  320x240, 352x288, 640x480 - verified live via Device.info.frame_sizes().
- camtest, board swapped to a Raspberry Pi 4B+, 2026-08-16: Microsoft
  USB3.0 HD CAMERA, USB id 045e:8888. `v4l2-ctl --list-formats-ext`
  confirmed this camera has **no MJPEG at all** - only YUYV, at
  1920x1080@30fps and 1280x720@60fps (no smaller sizes exist for it).

For an MJPEG-capable camera, no decode/re-encode happens: the camera
already produces MJPEG in hardware, so each frame linuxpy hands back
(confirmed to start with the JPEG SOI marker 0xFFD8) is forwarded to
CameraDriver.stream_mjpeg() unmodified - this is the "MJPEG passthrough"
half of the plan, contrasting with csi_driver.py, which always has to
decode+re-encode via PIL because Picamera2 has no native MJPEG capture
path.

For a YUYV-only camera, this driver decodes+re-encodes in software
itself (see _yuyv_to_rgb()/_reader_loop_yuyv()) rather than relying on
Picamera2/libcamera's uvcvideo pipeline handler to do it - confirmed
live that handler doesn't actually work for this: requesting RGB888,
BGR888, YUV420, or XRGB8888 from Picamera2 for a YUYV-only uvcvideo
camera all silently return the same raw 2-channel (H, W, 2) buffer
regardless of what format was asked for (no ISP/conversion behind that
pipeline handler, unlike bcm2835-isp for real CSI sensors) - so
camera_manager.py's build_camera_manager() deliberately does NOT let a
YUYV-only camera register through csi_driver.py even when Picamera2 can
see it (see that file's dedup logic) - it always comes through here
instead, where the conversion is actually implemented.

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
reopens the device on every single operation, unlike the first version
of this file. The device is closed on an explicit stop(), a genuine
resolution change, or the idle watchdog (see IDLE_STOP_SECONDS) - all
three still use STOP_START_SETTLE_SECONDS as a precaution, but these are
deliberately rare/debounced operations rather than something every photo
capture or tab switch triggers.

IDLE_STOP_SECONDS exists because the persistent thread above has a real,
measured cost: confirmed live that it burns ~100-130% of one CPU core
continuously on a Pi 4B+ (software YUYV->JPEG path) for as long as it's
running, regardless of whether anything is actually consuming
stream_mjpeg() - the frontend leaving the Camera tab doesn't stop it
(stopVideoFeed() is purely client-side, no backend call). The idle
watchdog trades back some of the max-robustness-to-disconnect stance
above for CPU/power savings, but only after a genuinely extended idle
period (not on every tab switch) - see the constant's own comment.
"""

from __future__ import annotations

import glob
import io
import os
import re
import threading
import time
from typing import Any, Iterator

from PIL import Image

from camera.camera_driver import CameraDriver

# Minimum pause between closing and reopening the device - see the module
# docstring for why this isn't optional on this hardware. Only exercised
# now by stop()+start() and genuine resolution changes, not by every
# capture_photo() call.
STOP_START_SETTLE_SECONDS = 0.5

# Conservative fps cap for the software YUYV->JPEG path (see
# _reader_loop_yuyv()). Benchmarked live on the Microsoft USB3.0 HD CAMERA
# hardware (a Pi 4B+, 4 cores): decode+re-encode alone averages ~100ms/frame
# at 1280x720 (~10fps ceiling on a single core) and ~240ms/frame at
# 1920x1080 (~4fps) - far below the camera's own advertised 30/60fps rates,
# which only describe raw YUYV capture, not producing a JPEG frame from it.
# MJPEG-capable cameras (pure passthrough, no conversion cost) aren't
# affected by this cap.
YUYV_MAX_FPS = 10

# JPEG quality used when re-encoding a software-converted YUYV frame -
# separate from any camera-side quality setting since there isn't one for
# this path (the camera never encodes anything itself).
YUYV_JPEG_QUALITY = 80

# How long capture_photo() waits for the background reader thread to
# deliver a first frame on a cold start, before giving up.
FIRST_FRAME_TIMEOUT_SECONDS = 3.0

# How long with zero active stream_mjpeg() consumers before the idle
# watchdog stops the reader thread and releases the device, trading the
# persistent-thread design's max-robustness-to-disconnect stance for
# CPU/power savings (confirmed live: the background reader thread alone
# costs ~100-130% of one CPU core continuously on a Pi 4B+ running the
# YUYV software-encode path, whether or not anyone's actually watching -
# see the project's usb-camera-plan notes). Deliberately NOT instant:
# leaving the Camera tab and coming back within this window (ordinary
# tab-switching) never triggers a stop/reopen cycle - only a genuinely
# extended idle period does, keeping real stop()/start() cycles rare
# rather than something every tab switch does, which is what this
# module's persistent-thread design exists to avoid in the first place
# (see the module docstring's E3500 fragility findings).
IDLE_STOP_SECONDS = 60

# Frame sizes this exact camera (Logitech QuickCam E 3500) was confirmed to
# support live via Device.info.frame_sizes() on camtest. Used as a fallback
# if runtime enumeration fails for any reason - this list is a known-good
# floor, not a guess.
CONFIRMED_RESOLUTIONS = ["160x120", "176x144", "320x240", "352x288", "640x480"]

DEFAULT_RESOLUTION = "640x480"
DEFAULT_FPS = 30


def _probe_pixel_formats(device) -> set[str]:
    """Pixel format names (e.g. {"YUYV"} or {"MJPEG", "YUYV"}) this
    already-open device's frame_sizes() advertises. Used both to decide
    MJPEG-passthrough vs. YUYV-software-encode in _apply_format(), and by
    camera_manager.py's build_camera_manager() to decide whether a camera
    Picamera2 also sees via uvcvideo actually has a working CSI path
    (MJPEG only - see this module's docstring for why YUYV doesn't work
    there)."""
    try:
        return {frame_size.pixel_format.name for frame_size in device.info.frame_sizes()}
    except Exception:
        return set()


def _yuyv_to_rgb(data: bytes, width: int, height: int):
    """Decode a raw YUYV (YUY2) 4:2:2 packed frame into an (H, W, 3) RGB
    uint8 array - vectorized with numpy (no per-pixel Python loop) since
    this runs once per frame on the reader thread. Standard BT.601
    limited-range coefficients, same ones ffmpeg/most V4L2 tooling use.

    Byte layout per pixel pair: Y0 U Y1 V (U/V shared between the pair).
    """
    import numpy as np

    yuyv = np.frombuffer(data, dtype=np.uint8).reshape(height, width // 2, 4).astype(np.int32)
    y0 = yuyv[..., 0]
    u = yuyv[..., 1] - 128
    y1 = yuyv[..., 2]
    v = yuyv[..., 3] - 128

    def _yuv_to_rgb(y, u, v):
        c = y - 16
        r = (298 * c + 409 * v + 128) >> 8
        g = (298 * c - 100 * u - 208 * v + 128) >> 8
        b = (298 * c + 516 * u + 128) >> 8
        return np.clip(r, 0, 255), np.clip(g, 0, 255), np.clip(b, 0, 255)

    r0, g0, b0 = _yuv_to_rgb(y0, u, v)
    r1, g1, b1 = _yuv_to_rgb(y1, u, v)

    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[:, 0::2, 0], rgb[:, 0::2, 1], rgb[:, 0::2, 2] = r0, g0, b0
    rgb[:, 1::2, 0], rgb[:, 1::2, 1], rgb[:, 1::2, 2] = r1, g1, b1
    return rgb


def _sizes_for_pixel_format(device, format_name: str) -> list[tuple[int, int]]:
    """(width, height) tuples this already-open device advertises for one
    pixel format, sorted smallest-first."""
    try:
        from linuxpy.video.device import PixelFormat

        target = PixelFormat[format_name]
    except (ImportError, KeyError):
        return []

    sizes: set[tuple[int, int]] = set()
    for frame_size in device.info.frame_sizes():
        if frame_size.pixel_format != target:
            continue
        info = frame_size.info
        width = getattr(info, "width", None)
        height = getattr(info, "height", None)
        if width and height:
            sizes.add((width, height))
    return sorted(sizes, key=lambda wh: wh[0] * wh[1])


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
                formats = _probe_pixel_formats(device)
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
            "formats": formats,
        })
    return found


class UsbCameraDriver(CameraDriver):
    def __init__(self, dev_path: str = "/dev/video0", card_name: str | None = None):
        self.dev_path = dev_path
        vendor_id, product_id = _usb_ids_for_video_device(dev_path)
        dev_name = os.path.basename(dev_path)
        self.id = f"usb:{vendor_id or 'unknown'}:{product_id or 'unknown'}:{dev_name}"
        self.display_name = "USB Camera"

        self._lock = threading.RLock()
        self._device = None  # linuxpy Device, opened lazily in start()
        self._started = False
        # discover_usb_cameras() already opens the device once to check
        # capabilities and reads .info.card while it's open - seeding it
        # here means a freshly-discovered driver's model is populated
        # immediately, without get_status() (used for the *active* driver
        # in CameraManager.list_drivers()) needing its own open/close probe.
        self._model = card_name or ""
        self._resolution = DEFAULT_RESOLUTION
        self._fps = DEFAULT_FPS
        # Set by _apply_format() once a device is actually opened - "MJPG"
        # (passthrough) or "YUYV" (software encode, see _reader_loop_yuyv()).
        # None until the first successful start().
        self._pixel_format: str | None = None
        self._stream_generation = 0
        self._reader_thread: threading.Thread | None = None
        self._idle_watchdog_thread: threading.Thread | None = None

        # Populated by _reader_loop(), read by stream_mjpeg()/capture_photo().
        # Separate from self._lock (which guards start/stop control flow)
        # so a long-lived stream_mjpeg() generator polling this never
        # blocks a concurrent stop() or capture_photo() call.
        self._frame_lock = threading.Lock()
        self._last_frame: bytes | None = None
        self._last_frame_time = 0.0

        # Tracks active stream_mjpeg() consumers for the idle watchdog (see
        # IDLE_STOP_SECONDS) - separate lock from _frame_lock since this is
        # consumer bookkeeping, not frame data.
        self._activity_lock = threading.Lock()
        self._active_consumers = 0
        self._last_activity_time = 0.0

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

                # _apply_format() sets self._resolution/self._fps/self._pixel_format
                # itself, since for a YUYV-only camera the actually-negotiated
                # values can differ from what was requested (e.g. this camera
                # has no 640x480 YUYV mode at all - see that method).
                self._started = True
                self._stream_generation += 1
                self._last_frame = None
                self._last_frame_time = 0.0

                generation = self._stream_generation
                reader_target = (
                    self._reader_loop_yuyv
                    if self._pixel_format == "YUYV"
                    else self._reader_loop_mjpeg
                )
                self._reader_thread = threading.Thread(
                    target=reader_target,
                    args=(self._device, generation),
                    name=f"usb-camera-reader-{generation}",
                    daemon=True,
                )
                self._reader_thread.start()

                with self._activity_lock:
                    self._active_consumers = 0
                    self._last_activity_time = time.time()
                self._idle_watchdog_thread = threading.Thread(
                    target=self._idle_watchdog_loop,
                    args=(generation,),
                    name=f"usb-camera-idle-watchdog-{generation}",
                    daemon=True,
                )
                self._idle_watchdog_thread.start()

                print(
                    f"[USB CAMERA] Started {self.dev_path} ({self._model}) "
                    f"at {self._resolution} {self._pixel_format}",
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
            self._stream_generation += 1  # tells _reader_loop()/watchdog to exit
            self._started = False
            thread = self._reader_thread
            self._reader_thread = None
            watchdog = self._idle_watchdog_thread
            self._idle_watchdog_thread = None
            device = self._device
            self._device = None

        # Join outside self._lock: the reader thread only ever touches
        # self._frame_lock, but holding self._lock here while waiting on
        # it would still needlessly block other callers (e.g. get_status())
        # for no reason.
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

        # The idle watchdog itself can be the one calling stop() (see
        # _idle_watchdog_loop()) - joining it from within itself would
        # deadlock, so only join when some other thread is doing the
        # stopping (an explicit /api/camera/power off, a driver switch, ...).
        if (
            watchdog is not None
            and watchdog.is_alive()
            and threading.current_thread() is not watchdog
        ):
            watchdog.join(timeout=2.0)

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
            "pixel_format": self._pixel_format,
            "last_frame_age_seconds": frame_age,
        }

    # ------------------------------------------------------------
    # CameraDriver
    # ------------------------------------------------------------

    def stream_mjpeg(self) -> Iterator[bytes]:
        if not self._started and not self.start():
            print("[USB CAMERA] Cannot start for streaming", flush=True)
            return

        with self._activity_lock:
            self._active_consumers += 1
        try:
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
        finally:
            # Only the last consumer to leave starts the idle clock - see
            # _idle_watchdog_loop(). Reader thread keeps running regardless
            # of consumer count; this only decides when it's OK to stop it.
            with self._activity_lock:
                self._active_consumers = max(0, self._active_consumers - 1)
                if self._active_consumers == 0:
                    self._last_activity_time = time.time()

    def capture_photo(self, resolution: str | None = None) -> bytes:
        """Shoots at the camera's real maximum resolution automatically
        (via _max_capture_resolution()) unless resolution is given
        explicitly - the user never picks a resolution for photos, see
        the module docstring's CAPTURE-AT-MAX-RESOLUTION note.

        If a live stream is already running at a different resolution,
        this briefly stops it, captures one frame at the target
        resolution, then restarts it at exactly what was running before -
        the only way to do this at all, since this hardware doesn't
        support two concurrent format/streaming contexts (confirmed live:
        a second open() succeeds, but set_format() on it fails with
        [Errno 16] Device or resource busy while the first is streaming).
        This uses the same stop()+reopen path _reconfigure() already used
        for a live resolution change, deliberately not used for ordinary
        streaming (see module docstring's E3500 fragility notes) - but a
        one-shot, user-triggered photo capture is a fundamentally
        different frequency/risk profile than continuous or repeated
        switching, and was verified live (repeated capture-while-streaming
        cycles, monitored via dmesg) before shipping - see the commit this
        landed in for that verification.

        Note for callers: this necessarily bumps _stream_generation twice
        (once to stop for the photo, once to resume streaming), which
        means any currently-open stream_mjpeg() consumer's loop condition
        will see the generation change and exit - an active /video_feed
        HTTP connection will end, not just pause. The frontend already
        handles this the same way it does for the old CSI capture path
        (dim + disable during capture, reconnect /video_feed afterward -
        see capturePhotoPreview() in chat.js).
        """
        with self._lock:
            target_resolution = (
                resolution or self._max_capture_resolution() or self._resolution
            )
            temporary_switch = self._started and target_resolution != self._resolution
            original_resolution = self._resolution
            original_fps = self._fps

            if temporary_switch:
                self.stop()
                time.sleep(STOP_START_SETTLE_SECONDS)

            if not self._started:
                if not self.start(resolution=target_resolution):
                    if temporary_switch:
                        # Best-effort: try to leave the stream as it was
                        # rather than leaving the camera fully stopped.
                        self.start(resolution=original_resolution, fps=original_fps)
                    return b""

        # Counts as activity even though it's not an open stream_mjpeg()
        # consumer - a burst of standalone photo captures shouldn't get
        # idle-stopped between shots (see _idle_watchdog_loop()).
        with self._activity_lock:
            if self._active_consumers == 0:
                self._last_activity_time = time.time()

        # Outside self._lock so start()/stop() aren't blocked by this wait -
        # same reasoning as before, now also covers the temporary-switch case.
        photo = self._wait_for_frame()

        if temporary_switch:
            with self._lock:
                self.stop()
                time.sleep(STOP_START_SETTLE_SECONDS)
                if not self.start(resolution=original_resolution, fps=original_fps):
                    print(
                        "[USB CAMERA] Failed to resume the live stream at "
                        f"{original_resolution} after photo capture - camera "
                        "left stopped, needs an explicit power cycle to recover",
                        flush=True,
                    )

        return photo

    def _wait_for_frame(self) -> bytes:
        """Wait for the background reader thread to deliver a frame -
        usually near-instant if already streaming at the target
        resolution, matters most right after a fresh start()."""
        deadline = time.time() + FIRST_FRAME_TIMEOUT_SECONDS
        while time.time() < deadline:
            with self._frame_lock:
                data = self._last_frame
            if data:
                return data
            time.sleep(0.03)

        print("[USB CAMERA] capture_photo() timed out waiting for a frame", flush=True)
        return b""

    def _max_capture_resolution(self) -> str | None:
        """Largest resolution this camera actually advertises (queried
        live via _probe_resolutions(), not assumed) - used by
        capture_photo() to shoot at the camera's real maximum
        automatically instead of whatever compromise resolution live
        streaming is using."""
        resolutions = self._probe_resolutions()
        if not resolutions:
            return None
        return resolutions[-1]

    def list_resolutions(self) -> list[str]:
        resolutions = self._probe_resolutions()
        return resolutions or list(CONFIRMED_RESOLUTIONS)

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _reader_loop_mjpeg(self, device, generation: int) -> None:
        """Runs on a background thread for the lifetime of one start()
        cycle - reads frames as fast as the camera produces them and
        stashes the latest one, so stream_mjpeg()/capture_photo() never
        have to touch the device directly. Pure passthrough: no decode/
        re-encode, see this module's docstring."""
        warned_bad_format = False
        try:
            for frame in device:
                if generation != self._stream_generation:
                    break
                data = bytes(frame)
                if not data:
                    continue

                # V4L2's set_format() ACKing "MJPG" doesn't guarantee the
                # bytes actually arriving are JPEG - confirmed live with a
                # second camera model (Logitech C170) that negotiates
                # MJPEG successfully at the ioctl level but streams
                # fixed-size, non-JPEG frames anyway (no SOI marker,
                # uniform size matching raw YUYV for the resolution, not
                # a plausible compressed size). Refuse to publish those:
                # leaving last_frame unset (and therefore this driver
                # looking "stuck, no frames") is a far more honest failure
                # mode than silently serving corrupt image data to
                # whatever's consuming stream_mjpeg()/capture_photo().
                if data[:2] != b"\xff\xd8":
                    if not warned_bad_format:
                        print(
                            f"[USB CAMERA] Dropping frame(s): does not start with "
                            f"the JPEG SOI marker (got {data[:4].hex()}) - camera "
                            "negotiated MJPEG but isn't actually sending it",
                            flush=True,
                        )
                        warned_bad_format = True
                    continue

                with self._frame_lock:
                    self._last_frame = data
                    self._last_frame_time = time.time()
        except Exception as error:
            if generation == self._stream_generation:
                print(f"[USB CAMERA] Reader thread error: {error}", flush=True)

    def _reader_loop_yuyv(self, device, generation: int) -> None:
        """Software fallback for cameras with no MJPEG at all (see this
        module's docstring for why Picamera2/libcamera can't be used for
        this either) - every frame is decoded from raw YUYV and re-encoded
        to JPEG here, unlike _reader_loop_mjpeg()'s pure passthrough."""
        try:
            width, height = (int(part) for part in self._resolution.split("x"))
        except (TypeError, ValueError):
            print(
                f"[USB CAMERA] Cannot start YUYV reader: bad resolution "
                f"{self._resolution!r}",
                flush=True,
            )
            return

        expected_bytes = width * height * 2
        warned_bad_size = False
        try:
            for frame in device:
                if generation != self._stream_generation:
                    break
                data = bytes(frame)
                if len(data) != expected_bytes:
                    if not warned_bad_size:
                        print(
                            f"[USB CAMERA] Dropping frame(s): expected "
                            f"{expected_bytes} bytes for {width}x{height} YUYV, "
                            f"got {len(data)}",
                            flush=True,
                        )
                        warned_bad_size = True
                    continue

                try:
                    rgb = _yuyv_to_rgb(data, width, height)
                    img = Image.fromarray(rgb)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=YUYV_JPEG_QUALITY)
                    jpeg_bytes = buf.getvalue()
                except Exception as convert_error:
                    print(
                        f"[USB CAMERA] YUYV->JPEG conversion failed: {convert_error}",
                        flush=True,
                    )
                    continue

                with self._frame_lock:
                    self._last_frame = jpeg_bytes
                    self._last_frame_time = time.time()
        except Exception as error:
            if generation == self._stream_generation:
                print(f"[USB CAMERA] Reader thread error: {error}", flush=True)

    def _idle_watchdog_loop(self, generation: int) -> None:
        """Stops the reader thread (and releases the device) after
        IDLE_STOP_SECONDS with zero active stream_mjpeg() consumers - see
        that constant's own comment for the CPU/power tradeoff this makes.
        Runs on its own thread, separate from _reader_thread, specifically
        so it can call self.stop() without a self-join deadlock (stop()
        checks for that explicitly too, as a second guard)."""
        while generation == self._stream_generation:
            time.sleep(5)
            if generation != self._stream_generation:
                break

            with self._activity_lock:
                if self._active_consumers > 0:
                    idle_seconds = 0.0
                else:
                    idle_seconds = time.time() - self._last_activity_time

            if idle_seconds >= IDLE_STOP_SECONDS:
                print(
                    f"[USB CAMERA] Idle for {idle_seconds:.0f}s with no "
                    "active viewers - stopping to save CPU/power",
                    flush=True,
                )
                self.stop()
                break

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

            available = _probe_pixel_formats(device)
            if "MJPEG" in available:
                pixel_format = "MJPG"
            elif "YUYV" in available:
                pixel_format = "YUYV"
            else:
                print(
                    f"[USB CAMERA] No usable pixel format among {sorted(available) or 'none reported'}",
                    flush=True,
                )
                return False

            if pixel_format == "YUYV":
                sizes = _sizes_for_pixel_format(device, "YUYV")
                if sizes and (width, height) not in sizes:
                    # No MJPEG on this camera means no small discrete sizes
                    # either, in the one case tested live (a Microsoft
                    # USB3.0 HD CAMERA only offers 1280x720/1920x1080) -
                    # pick the smallest available instead of letting
                    # set_format() silently round to whatever the driver
                    # feels like.
                    width, height = sizes[0]
                    print(
                        f"[USB CAMERA] {resolution} not available in YUYV, "
                        f"using {width}x{height} instead",
                        flush=True,
                    )
                if fps > YUYV_MAX_FPS:
                    # See YUYV_MAX_FPS's own comment - every frame is
                    # decoded+re-encoded here, so the camera's advertised
                    # fps (which only describes raw capture) isn't
                    # achievable in practice.
                    print(
                        f"[USB CAMERA] Capping fps to {YUYV_MAX_FPS} for the "
                        f"software YUYV->JPEG path (requested {fps})",
                        flush=True,
                    )
                    fps = YUYV_MAX_FPS

            device.set_format(BufferType.VIDEO_CAPTURE, width, height, pixel_format)
            try:
                device.set_fps(BufferType.VIDEO_CAPTURE, fps)
            except Exception as fps_error:
                # Frame rate is a nice-to-have; capture at the camera's
                # default fps for this resolution still works.
                print(f"[USB CAMERA] set_fps() failed, continuing without it: {fps_error}", flush=True)

            self._resolution = f"{width}x{height}"
            self._fps = fps
            self._pixel_format = pixel_format
            return True
        except Exception as error:
            print(f"[USB CAMERA] Format negotiation failed ({width}x{height}): {error}", flush=True)
            return False

    def _reconfigure(self, resolution: str, fps: int) -> bool:
        # Only reached for a genuine resolution/fps change - normal
        # streaming and photo capture never call stop()+start() anymore.
        self.stop()
        time.sleep(STOP_START_SETTLE_SECONDS)
        return self.start(resolution=resolution, fps=fps)

    def _probe_resolutions(self) -> list[str]:
        try:
            from linuxpy.video.device import Device as V4LDevice
        except ImportError:
            return []

        try:
            device = V4LDevice(self.dev_path)
            device.open()
            try:
                available = _probe_pixel_formats(device)
                # Prefer MJPEG sizes (what actually gets negotiated - see
                # _apply_format()); only fall back to YUYV sizes for a
                # camera that has no MJPEG at all.
                pixel_format = "MJPEG" if "MJPEG" in available else "YUYV"
                sizes = _sizes_for_pixel_format(device, pixel_format)
                return [f"{w}x{h}" for w, h in sizes]
            finally:
                device.close()
        except Exception as error:
            print(f"[USB CAMERA] Resolution probe failed, using confirmed fallback list: {error}", flush=True)
            return []
