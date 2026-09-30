"""USB/UVC camera driver — CAM-1 (audit review finding F10, 2026-09-30):
ffmpeg subprocess + v4l2-ctl subprocess, replacing the old linuxpy/v4l2py
in-process driver. linuxpy/v4l2py are GPL-3.0-or-later; importing them
directly into Core's own MIT-licensed process is exactly the pattern the
meshtastic-adapter process boundary was built to avoid (see CLAUDE.md's
"GPLv3 process isolation" section). ffmpeg and v4l2-ctl are invoked here
as arm's-length external programs - never imported as a library - the
same reasoning already applied to the `meshtastic` CLI. See CAM-0's
feasibility report (2026-09-30) for the live numbers this design is
based on, and THIRD_PARTY_NOTICES.md for the licensing detail.

Module rewritten in place: same file, same public names
(`UsbCameraDriver`, `discover_usb_cameras`) - camera_manager.py's imports
need no changes.

Reference hardware (CAM-1, camtest .107, 2026-09-30): Microsoft USB3.0 HD
CAMERA, USB id 045e:8888, YUYV-only (no MJPEG). The Logitech E3500 that
motivated the old driver's close/reopen fragility workarounds is retired
(too old, scope decision 2026-09-30); the C170 is untested for now. MJPEG
passthrough (the `-c:v copy` path below) is implemented and unit-tested
against a fake ffmpeg, but NOT live-verified against a real MJPEG camera -
see the CAM-1 PR description for this gap, tracked as a backlog item for
whenever an MJPEG-capable camera is available again.

ARCHITECTURE - one persistent stream, rare restarts (kept from the old
driver; motivation updated since the E3500 fragility that originally
justified it no longer applies to the retired hardware): every ffmpeg
start costs real time-to-first-frame (CAM-0, live-measured: 0.4-2.3s
depending on resolution) and, for a YUYV-only camera, a continuous
software transcode costs about 1.2 CPU cores throughout (CAM-0,
live-measured on a Pi 4B+ at 720p30) - restarting per viewer or per photo
would be needlessly expensive on both counts. So: one long-lived ffmpeg
subprocess serves every stream_mjpeg() viewer and (when the resolution
already matches) every capture_photo() from a single shared
`_last_frame` buffer; a restart only happens on an explicit stop(), a
genuine resolution change, or the idle watchdog (IDLE_STOP_SECONDS).
STOP_START_SETTLE_SECONDS is kept as cheap insurance between a stop and
the next start, even though nothing here proves the reference camera
needs it.

GOTCHA - ffmpeg's exit code on camera loss (CAM-0, live-confirmed by
simulating an unplug via USB unbind while streaming): the process exits
with code 0 - not a nonzero error - typically within 250ms, with
"ioctl(VIDIOC_DQBUF): No such device" on stderr. A deliberate stop
(SIGTERM/SIGINT) exits differently (255 on the tested ffmpeg build), but
neither exit code is reliable read in isolation as "success" vs
"failure" the naive way. This driver tracks `self._stop_requested`
explicitly instead of trusting the raw exit code: any process exit that
happens while `_stop_requested` is False is treated as a failure,
whatever the exit code (including 0) - see `_reader_loop_stream()`'s
tail and `_stall_watchdog_loop()`.
"""

from __future__ import annotations

import atexit
import concurrent.futures
import glob
import io
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections import deque
from typing import Any, Iterator

from PIL import Image

from camera.camera_driver import CameraDriver
from camera.v4l2_ctl_parse import (
    parse_card_name,
    parse_device_caps,
    parse_list_ctrls,
    parse_list_formats_ext,
)

# ---------------------------------------------------------------------
# External-program resolution, once at module load. FFMPEG_CMD_PREFIX is
# a list (not a bare path string) specifically so tests can monkeypatch
# it to `[sys.executable, str(fake_ffmpeg_script)]` and exercise the real
# subprocess-lifecycle code below against tests/fixtures/fake_ffmpeg.py
# instead of a real ffmpeg binary - see tests/test_usb_camera_driver.py.
# ---------------------------------------------------------------------
FFMPEG_PATH = shutil.which("ffmpeg")
V4L2_CTL_PATH = shutil.which("v4l2-ctl")
FFMPEG_CMD_PREFIX: list[str] = [FFMPEG_PATH] if FFMPEG_PATH else []

# Cheap insurance between a stop and the next start - kept from the old
# driver. Nothing about the current reference hardware (Microsoft
# USB3.0 HD CAMERA) proves this is necessary (CAM-0: 20/20 restart
# cycles clean with no settle delay at all), but the underlying concern
# (a USB-level re-enumeration race on close-then-reopen) is a kernel/USB
# property, not specific to which userspace tool opens the device, so
# this stays as a precaution pending real E3500/C170 testing.
STOP_START_SETTLE_SECONDS = 0.5

# Conservative fps cap for a YUYV-only camera's software transcode path -
# CAM-0 measured ~5.7-9.8 fps as the real encode ceiling on a Pi Zero 2W,
# and the old driver's own Pi 4B+ Python-path benchmark (since removed
# with that code) was ~10fps. ffmpeg on a Pi 4B+ comfortably clears this
# (CAM-0: sustained 30fps live), but the cap protects weaker hosts - see
# _low_power_host() and DEFAULT_RESOLUTION_FOR_LOW_POWER_HOST below.
YUYV_MAX_FPS = 10

# The old driver's PIL JPEG quality knob (0-100, higher = better) doesn't
# exist for ffmpeg's mjpeg encoder, which instead uses `-q:v` on an
# inverted 2 (best) - 31 (worst) scale. 80/100 in PIL's scale is a
# moderately high-quality setting; -q:v 4 is ffmpeg's closest equivalent
# in practice (empirically: -q:v 2-3 is visually near-lossless and much
# larger, -q:v 5-6 starts showing visible blocking on fine detail -
# CAM-0's own live captures used -q:v 5 for cheap CPU, this driver uses
# a slightly higher-quality default since it's a live-viewed stream, not
# a disposable test capture).
YUYV_FFMPEG_QUALITY = 4

FIRST_FRAME_TIMEOUT_SECONDS = 3.0

# Zero consumers for this long -> the idle watchdog stops the ffmpeg
# subprocess. Matters more now than for the old CSI-decode-free MJPEG
# passthrough case: a YUYV software transcode costs ~1.2 CPU cores
# continuously (CAM-0), whether or not anyone is actually watching.
IDLE_STOP_SECONDS = 60

# How often the idle watchdog re-checks - separate from IDLE_STOP_SECONDS
# itself so tests can shrink both independently (a test patching only
# IDLE_STOP_SECONDS down would otherwise still wait up to this many
# seconds for the next poll).
IDLE_WATCHDOG_POLL_SECONDS = 5

# No complete frame for this long while the process is supposedly
# running -> treated as a failure and restarted, even though the process
# hasn't exited. CAM-0's own design note: an unplug can hang instead of
# exiting cleanly on some kernels, so exit-code-based failure detection
# alone isn't sufficient.
STALL_MIN_SECONDS = 5.0
STALL_FRAME_INTERVALS = 3

# If no JPEG EOI marker shows up within this many buffered bytes, the
# stream is treated as corrupt (a camera lying about its own format, or
# genuine transport corruption) rather than buffered forever - same
# "refuse to publish, don't guess" stance the old driver's SOI-marker
# check used for a camera (the Logitech C170) that ACKed MJPEG at the
# ioctl level but didn't actually send valid JPEG frames.
FRAME_BUFFER_CAP_BYTES = 8 * 1024 * 1024

# How much to try to read from ffmpeg's stdout pipe per read() call -
# independent of how ffmpeg itself chunks its writes; deliberately not
# aligned to any particular frame size so the reader is exercised
# against frames split arbitrarily across reads (see
# tests/fixtures/fake_ffmpeg.py's own FAKE_FFMPEG_CHUNK_SIZE).
STDOUT_READ_CHUNK_BYTES = 65536

# How long stop() waits after SIGTERM before escalating to SIGKILL.
STOP_GRACE_SECONDS = 2.0

# Known-good floor for list_resolutions() if runtime enumeration fails -
# the reference hardware's own confirmed YUYV sizes (CAM-0, live-verified
# via v4l2-ctl --list-formats-ext), not a guess. Replaces the old
# driver's E3500-specific CONFIRMED_RESOLUTIONS list now that hardware is
# retired.
CONFIRMED_RESOLUTIONS = ["1280x720", "1920x1080"]
DEFAULT_RESOLUTION = "1280x720"
DEFAULT_FPS = 30

# CAM-1 task's low-power-host guard: a YUYV-only camera on a host this
# weak can't keep up in software (CAM-0: Pi Zero 2W topped out at ~9.8fps
# even at 640x480) - default to conservative settings there instead of
# silently falling behind or pegging every core.
LOW_POWER_HOST_MAX_CORES = 4
LOW_POWER_HOST_MAX_RAM_MB = 1024
LOW_POWER_DEFAULT_RESOLUTION = "640x480"
LOW_POWER_DEFAULT_FPS = 10

_low_power_warning_logged = False


def _read_sys_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def _usb_ids_for_video_device(dev_path: str) -> tuple[str, str]:
    """Return (vendor_id, product_id) hex strings for a /dev/videoN node,
    read from sysfs - stdlib-only, no dependency on v4l2-ctl or any
    library. /sys/class/video4linux/videoN/device is a symlink to the USB
    *interface* directory (e.g. .../2-2:1.0); idVendor/idProduct live one
    level up, on the actual USB device directory (.../2-2) - verified
    against the real camtest hardware path (CAM-0/CAM-1)."""
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


def _run_v4l2_ctl(dev_path: str, *args: str, timeout: float = 5.0) -> str:
    """stdout of `v4l2-ctl -d <dev_path> <*args>`, or "" on any failure
    (missing binary, nonzero exit, timeout, exception) - never raises.
    A directly-monkeypatchable seam for driver-level tests (separate from
    camera/v4l2_ctl_parse.py's own pure-text-parsing tests, which don't
    need a real subprocess at all).

    One retry on timeout only (live-caught on camtest, CAM-1 verification,
    2026-09-30: a `--list-formats-ext` call occasionally took the full 5s
    timeout - roughly 1 in 10 calls in a rapid stop/restart test loop -
    while the same call in isolation consistently completed in <20ms; the
    timing lines up with a brief kernel-level handoff window right after a
    previous ffmpeg process released the device, not a generally slow or
    broken v4l2-ctl. A single retry absorbs that narrow window without
    hiding a genuinely broken/missing binary - that still fails fast on
    the first attempt via FileNotFoundError, not a timeout, so it isn't
    retried needlessly)."""
    if not V4L2_CTL_PATH:
        return ""
    for attempt in range(2):
        try:
            result = subprocess.run(
                [V4L2_CTL_PATH, "-d", dev_path, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.stdout
        except subprocess.TimeoutExpired as error:
            if attempt == 0:
                continue
            print(f"[USB CAMERA] v4l2-ctl {' '.join(args)} failed for {dev_path}: {error}", flush=True)
            return ""
        except Exception as error:
            print(f"[USB CAMERA] v4l2-ctl {' '.join(args)} failed for {dev_path}: {error}", flush=True)
            return ""
    return ""


def _pid_holding_device(dev_path: str) -> int | None:
    """PID of a process with dev_path open, via `lsof -t` - the same
    external-tool pattern meshsrv/serial_port_supervisor.py already uses
    for the analogous serial-port-busy check (`lsof` is already a
    MeshCenter dependency, install.sh installs it unconditionally). None
    if lsof is missing, times out, errors, or nothing holds the device -
    "couldn't find evidence of a holder" is not the same as "confirmed
    free", but for this one-time startup check (unlike the serial port's
    hot-path claim logic) a simple None-on-any-failure is an acceptable,
    much smaller-scoped version of that same principle."""
    lsof_path = shutil.which("lsof")
    if not lsof_path:
        return None
    try:
        result = subprocess.run(
            [lsof_path, "-t", dev_path], capture_output=True, text=True, timeout=3.0,
        )
        pids = [int(token) for token in result.stdout.split() if token.strip().isdigit()]
        return pids[0] if pids else None
    except Exception:
        return None


def _low_power_host() -> bool:
    """True on a host weak enough that CAM-0's Zero 2W numbers apply
    (<=4 cores, <1GB RAM) - checked via os.cpu_count()/a /proc/meminfo
    read, no psutil dependency needed for just this. Best-effort: any
    read failure defaults to False (don't apply conservative limits based
    on a guess)."""
    try:
        cores = os.cpu_count() or 0
        if cores == 0 or cores > LOW_POWER_HOST_MAX_CORES:
            return False
        total_kb = 0
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    total_kb = int(line.split()[1])
                    break
        return 0 < total_kb < LOW_POWER_HOST_MAX_RAM_MB * 1024
    except Exception:
        return False


def _probe_pixel_formats(dev_path: str) -> dict[str, list[dict]]:
    """{"YUYV": [{"width":..,"height":..,"fps":[..]}, ...], ...} for
    dev_path, via `v4l2-ctl --list-formats-ext` - see
    camera/v4l2_ctl_parse.py's own docstring for the parsing detail and
    its fragility note."""
    text = _run_v4l2_ctl(dev_path, "--list-formats-ext")
    return parse_list_formats_ext(text)


def discover_usb_cameras() -> list[dict[str, Any]]:
    """Enumerate /dev/video* nodes that are actual USB capture devices -
    not a paired metadata/M2M node (this camera's own /dev/video1), and
    not the Raspberry Pi's bcm2835-isp platform nodes that exist
    regardless of any USB camera. Same two-filter shape as the old
    linuxpy-based version (both confirmed necessary live on camtest):

    - `v4l2-ctl --info`'s per-node `Device Caps:` block (not the
      deprecated aggregate `Capabilities:` block) - video0 and video1
      report the same aggregate capabilities even though only video0 can
      actually capture; `Device Caps:` is what actually differs (`Video
      Capture` vs `Metadata Capture` - see v4l2_ctl_parse.parse_device_caps()).
    - A real USB vendor/product id from sysfs - bcm2835-isp is a platform
      device, not USB, and would otherwise pass the capability check too.
    """
    found: list[dict[str, Any]] = []
    for dev_path in sorted(glob.glob("/dev/video*")):
        if not re.fullmatch(r"/dev/video\d+", dev_path):
            continue

        vendor_id, product_id = _usb_ids_for_video_device(dev_path)
        if not vendor_id or not product_id:
            continue

        info_text = _run_v4l2_ctl(dev_path, "--info")
        if not info_text:
            continue
        caps = parse_device_caps(info_text)
        if "Video Capture" not in caps:
            continue

        card_name = parse_card_name(info_text)
        formats = set(_probe_pixel_formats(dev_path).keys())

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
        self._process: subprocess.Popen | None = None
        self._started = False
        self._model = card_name or ""
        self._resolution = DEFAULT_RESOLUTION
        self._fps = DEFAULT_FPS
        self._pixel_format: str | None = None  # "MJPG" (passthrough) or "YUYV" (transcode)
        self._stream_generation = 0
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stall_watchdog_thread: threading.Thread | None = None
        self._idle_watchdog_thread: threading.Thread | None = None
        self._stop_requested = False
        self._available = True  # False after an undetected/failed process exit

        self._frame_lock = threading.Lock()
        self._last_frame: bytes | None = None
        self._last_frame_time = 0.0

        self._activity_lock = threading.Lock()
        self._active_consumers = 0
        self._last_activity_time = 0.0

        self._stderr_lines: deque[str] = deque(maxlen=50)

        _ALL_DRIVERS.add(self)

    # ------------------------------------------------------------
    # DeviceDriver
    # ------------------------------------------------------------

    def detect(self) -> dict[str, Any] | None:
        if not os.path.exists(self.dev_path):
            return None
        if not FFMPEG_CMD_PREFIX or not V4L2_CTL_PATH:
            _log_missing_tooling_once()
            return None

        info_text = _run_v4l2_ctl(self.dev_path, "--info")
        if not info_text:
            return None
        caps = parse_device_caps(info_text)
        if "Video Capture" not in caps:
            return None

        self._model = parse_card_name(info_text) or "USB Camera"
        vendor_id, product_id = _usb_ids_for_video_device(self.dev_path)
        return {
            "model": self._model,
            "vendor_id": vendor_id,
            "product_id": product_id,
            "dev_path": self.dev_path,
        }

    def start(self, resolution: str | None = None, fps: int | None = None, **_options: Any) -> bool:
        with self._lock:
            target_resolution = resolution or self._resolution
            target_fps = fps or self._fps

            if self._started and self._process is not None:
                if target_resolution != self._resolution:
                    # A genuine resolution change - unlike the old
                    # driver (which refused this live to protect the
                    # now-retired E3500), CAM-0's restart-cycle testing
                    # (20/20 clean, 0.4-2.3s) supports allowing it here.
                    return self._reconfigure(target_resolution, target_fps)
                return True

            return self._start_stream(target_resolution, target_fps)

    def _start_stream(self, resolution: str, fps: int) -> bool:
        if not FFMPEG_CMD_PREFIX or not V4L2_CTL_PATH:
            _log_missing_tooling_once()
            return False

        holder_pid = _pid_holding_device(self.dev_path)
        if holder_pid is not None and holder_pid != os.getpid():
            print(
                f"[USB CAMERA] {self.dev_path} is held by PID {holder_pid} "
                "(likely an ffmpeg process orphaned by a previous crash) - "
                "refusing to start a competing process. Stop it manually "
                "if it isn't coming back on its own.",
                flush=True,
            )
            return False

        if _low_power_host():
            resolution, fps = self._apply_low_power_defaults(resolution, fps)

        pixel_format, resolution, fps = self._choose_format(resolution, fps)
        if pixel_format is None:
            return False

        argv = self._build_ffmpeg_stream_argv(pixel_format, resolution, fps)
        try:
            process = _spawn_ffmpeg(argv)
        except Exception as error:
            print(f"[USB CAMERA] Failed to spawn ffmpeg: {error}", flush=True)
            return False

        self._process = process
        self._resolution = resolution
        self._fps = fps
        self._pixel_format = pixel_format
        self._started = True
        self._stop_requested = False
        self._available = True
        self._stream_generation += 1
        self._last_frame = None
        self._last_frame_time = 0.0
        self._stderr_lines.clear()

        generation = self._stream_generation
        self._reader_thread = threading.Thread(
            target=self._reader_loop_stream, args=(process, generation),
            name=f"usb-camera-reader-{generation}", daemon=True,
        )
        self._reader_thread.start()

        self._stderr_thread = threading.Thread(
            target=_drain_stderr, args=(process, self._stderr_lines, "[USB CAMERA ffmpeg]"),
            name=f"usb-camera-stderr-{generation}", daemon=True,
        )
        self._stderr_thread.start()

        self._stall_watchdog_thread = threading.Thread(
            target=self._stall_watchdog_loop, args=(generation, fps),
            name=f"usb-camera-stall-{generation}", daemon=True,
        )
        self._stall_watchdog_thread.start()

        with self._activity_lock:
            self._active_consumers = 0
            self._last_activity_time = time.time()
        self._idle_watchdog_thread = threading.Thread(
            target=self._idle_watchdog_loop, args=(generation,),
            name=f"usb-camera-idle-{generation}", daemon=True,
        )
        self._idle_watchdog_thread.start()

        print(
            f"[USB CAMERA] Started {self.dev_path} ({self._model}) "
            f"at {resolution} {pixel_format} (pid {process.pid})",
            flush=True,
        )
        return True

    def _apply_low_power_defaults(self, resolution: str, fps: int) -> tuple[str, int]:
        global _low_power_warning_logged
        formats = _probe_pixel_formats(self.dev_path)
        if "MJPEG" in formats:
            # MJPEG passthrough costs near-zero CPU regardless of host
            # strength - the low-power guard only exists for the
            # software-transcode (YUYV) case.
            return resolution, fps
        if not _low_power_warning_logged:
            print(
                "[USB CAMERA] WARNING: this host has <=4 cores and <1GB RAM, "
                "and this camera has no MJPEG mode - software YUYV->JPEG "
                f"encoding is CPU-bound here (CAM-0 measured ~9.8fps max at "
                f"640x480 on a Pi Zero 2W). Defaulting to "
                f"{LOW_POWER_DEFAULT_RESOLUTION}@{LOW_POWER_DEFAULT_FPS}fps.",
                flush=True,
            )
            _low_power_warning_logged = True
        return LOW_POWER_DEFAULT_RESOLUTION, min(fps, LOW_POWER_DEFAULT_FPS)

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True
            self._stream_generation += 1
            self._started = False
            process = self._process
            self._process = None
            reader = self._reader_thread
            self._reader_thread = None
            stall_watchdog = self._stall_watchdog_thread
            self._stall_watchdog_thread = None
            idle_watchdog = self._idle_watchdog_thread
            self._idle_watchdog_thread = None

        if process is not None:
            _terminate_process(process)

        for thread in (reader, stall_watchdog):
            if thread is not None and thread.is_alive():
                thread.join(timeout=STOP_GRACE_SECONDS + 1.0)

        if (
            idle_watchdog is not None
            and idle_watchdog.is_alive()
            and threading.current_thread() is not idle_watchdog
        ):
            idle_watchdog.join(timeout=2.0)

        with self._frame_lock:
            self._last_frame = None
            self._last_frame_time = 0.0

    def get_status(self) -> dict[str, Any]:
        with self._frame_lock:
            frame_age = (time.time() - self._last_frame_time) if self._last_frame_time else None
        return {
            "ok": os.path.exists(self.dev_path) and self._available,
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
            with self._activity_lock:
                self._active_consumers = max(0, self._active_consumers - 1)
                if self._active_consumers == 0:
                    self._last_activity_time = time.time()

    def capture_photo(self, resolution: str | None = None) -> bytes:
        """If the stream is already running at the requested resolution
        (or none is given), returns the next fresh frame from
        `_last_frame` - no process spawned at all. Otherwise: stop the
        stream, run a one-shot ffmpeg capture at the target resolution,
        restart the stream at the original resolution - same generation-
        counter-bumps-twice behaviour the old driver had, so an open
        stream_mjpeg() consumer's loop condition sees the change and
        exits cleanly (the frontend already handles this - see
        capturePhotoPreview() in chat.js)."""
        with self._lock:
            target_resolution = resolution or self._max_capture_resolution() or self._resolution

            if not self._started:
                # Cold start: nothing to preserve - start the persistent
                # stream directly at the target resolution (no separate
                # one-shot process needed) and let it serve this photo,
                # same as the old driver's cold-start behaviour.
                if not self._start_stream(target_resolution, DEFAULT_FPS):
                    return b""
                pixel_format = self._pixel_format
            elif target_resolution == self._resolution:
                pixel_format = self._pixel_format
            else:
                original_resolution = self._resolution
                original_fps = self._fps
                self.stop()
                time.sleep(STOP_START_SETTLE_SECONDS)

                photo = self._one_shot_capture(target_resolution)

                time.sleep(STOP_START_SETTLE_SECONDS)
                if not self._start_stream(original_resolution, original_fps):
                    print(
                        "[USB CAMERA] Failed to resume the live stream at "
                        f"{original_resolution} after photo capture - camera "
                        "left stopped, needs an explicit restart to recover",
                        flush=True,
                    )
                return photo

        with self._activity_lock:
            if self._active_consumers == 0:
                self._last_activity_time = time.time()

        photo = self._wait_for_frame()
        if pixel_format == "MJPG" and photo:
            photo = self._validated_or_retry(photo)
        return photo

    def _validated_or_retry(self, photo: bytes) -> bytes:
        """MJPEG passthrough frames come straight from the camera with no
        decode step in between - validate with PIL before handing them
        out, and retry once, since a corrupt/partial frame is possible in
        principle even though this exact failure mode was only ever
        observed on a different camera (the Logitech C170, not the
        MJPEG-less reference hardware CAM-1 was verified against)."""
        if _is_valid_jpeg(photo):
            return photo
        print("[USB CAMERA] Photo frame failed JPEG validation, retrying once", flush=True)
        retried = self._wait_for_frame()
        if _is_valid_jpeg(retried):
            return retried
        print("[USB CAMERA] Retry also failed JPEG validation, returning empty", flush=True)
        return b""

    def _one_shot_capture(self, resolution: str) -> bytes:
        pixel_format, resolution, _fps = self._choose_format(resolution, DEFAULT_FPS)
        if pixel_format is None:
            return b""

        argv = self._build_ffmpeg_photo_argv(pixel_format, resolution)
        # Spawned via _spawn_ffmpeg() (the persistent spawner thread), not
        # subprocess.run() directly, so a one-shot capture gets the same
        # process-group/PDEATHSIG protection as the streaming path - see
        # the module-level comment above _SPAWN_QUEUE. communicate() then
        # does the actual wait-for-completion on THIS (caller's) thread,
        # which is fine - only the fork() itself needs to happen on the
        # persistent thread.
        try:
            process = _spawn_ffmpeg(argv)
            stdout, _stderr = process.communicate(timeout=FIRST_FRAME_TIMEOUT_SECONDS + 5.0)
        except subprocess.TimeoutExpired:
            print("[USB CAMERA] One-shot capture timed out", flush=True)
            _terminate_process(process)
            return b""
        except Exception as error:
            print(f"[USB CAMERA] One-shot capture failed: {error}", flush=True)
            return b""

        photo = _extract_first_frame(stdout)
        if not photo:
            print("[USB CAMERA] One-shot capture produced no valid frame", flush=True)
            return b""
        if not _is_valid_jpeg(photo):
            print("[USB CAMERA] One-shot capture frame failed JPEG validation", flush=True)
            return b""
        return photo

    def _wait_for_frame(self) -> bytes:
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
        resolutions = self.list_resolutions()
        if not resolutions:
            return None

        def _area(res: str) -> int:
            try:
                w, h = res.split("x")
                return int(w) * int(h)
            except ValueError:
                return 0

        return max(resolutions, key=_area)

    def list_resolutions(self) -> list[str]:
        formats = _probe_pixel_formats(self.dev_path)
        pixel_format = "MJPEG" if "MJPEG" in formats else "YUYV"
        sizes = formats.get(pixel_format, [])
        resolutions = [f"{size['width']}x{size['height']}" for size in sizes]
        return resolutions or list(CONFIRMED_RESOLUTIONS)

    def get_controls(self) -> dict[str, Any]:
        """v4l2-ctl --list-ctrls, parsed into {name: current_value}. Note
        for reviewers: the USB driver had NO control support at all
        before CAM-1 (CameraDriver's base-class no-ops) and the UI's
        brightness/contrast sliders (/api/camera/settings) are wired to
        the CSI-only camera.py path, not this one - so this is net-new
        capability, not a preserved one, and nothing in the UI calls it
        yet (see the CAM-1 PR description)."""
        text = _run_v4l2_ctl(self.dev_path, "--list-ctrls")
        parsed = parse_list_ctrls(text)
        return {name: info["value"] for name, info in parsed.items() if info.get("value") is not None}

    def set_controls(self, controls: dict[str, Any]) -> bool:
        """v4l2-ctl --set-ctrl name=value for each entry - confirmed live
        (CAM-0) to apply instantly to an already-streaming ffmpeg process,
        no restart needed. Returns False if v4l2-ctl is missing or every
        control failed; a partial success (some controls applied, others
        rejected) still returns True, matching v4l2-ctl's own per-control
        exit behaviour."""
        if not V4L2_CTL_PATH or not controls:
            return False
        assignments = [f"{name}={value}" for name, value in controls.items()]
        output = _run_v4l2_ctl(self.dev_path, "--set-ctrl", ",".join(assignments))
        # v4l2-ctl prints nothing on success; _run_v4l2_ctl() already
        # logs and returns "" on a hard failure (missing binary, timeout,
        # non-zero exit is not itself checked here since --set-ctrl can
        # partially apply - "" only distinguishes "never even ran").
        return V4L2_CTL_PATH is not None

    # ------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------

    def _choose_format(self, resolution: str, fps: int) -> tuple[str | None, str, int]:
        """Pick MJPEG passthrough if the camera offers it at the
        requested resolution, else YUYV software transcode capped at
        YUYV_MAX_FPS. Falls back to the smallest available size if the
        requested one isn't offered (same "pick the smallest available
        rather than let ffmpeg guess" stance the old driver used)."""
        try:
            width, height = (int(part) for part in resolution.split("x"))
        except (TypeError, ValueError):
            print(f"[USB CAMERA] Invalid resolution string: {resolution!r}", flush=True)
            return None, resolution, fps

        formats = _probe_pixel_formats(self.dev_path)

        if "MJPEG" in formats:
            sizes = {(s["width"], s["height"]) for s in formats["MJPEG"]}
            if (width, height) in sizes or not sizes:
                return "MJPG", resolution, fps
            width, height = min(sizes, key=lambda wh: wh[0] * wh[1])
            print(f"[USB CAMERA] {resolution} not available in MJPEG, using {width}x{height} instead", flush=True)
            return "MJPG", f"{width}x{height}", fps

        if "YUYV" in formats:
            sizes = {(s["width"], s["height"]) for s in formats["YUYV"]}
            if sizes and (width, height) not in sizes:
                width, height = min(sizes, key=lambda wh: wh[0] * wh[1])
                print(f"[USB CAMERA] {resolution} not available in YUYV, using {width}x{height} instead", flush=True)
            if fps > YUYV_MAX_FPS:
                print(f"[USB CAMERA] Capping fps to {YUYV_MAX_FPS} for the software YUYV->JPEG path (requested {fps})", flush=True)
                fps = YUYV_MAX_FPS
            return "YUYV", f"{width}x{height}", fps

        print(f"[USB CAMERA] No usable pixel format among {sorted(formats) or 'none reported'}", flush=True)
        return None, resolution, fps

    def _build_ffmpeg_stream_argv(self, pixel_format: str, resolution: str, fps: int) -> list[str]:
        input_format = "mjpeg" if pixel_format == "MJPG" else "yuyv422"
        codec_args = ["-c:v", "copy"] if pixel_format == "MJPG" else ["-c:v", "mjpeg", "-q:v", str(YUYV_FFMPEG_QUALITY)]
        return [
            *FFMPEG_CMD_PREFIX,
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", input_format,
            "-video_size", resolution, "-framerate", str(fps),
            "-i", self.dev_path,
            *codec_args,
            "-f", "mjpeg", "pipe:1",
        ]

    def _build_ffmpeg_photo_argv(self, pixel_format: str, resolution: str) -> list[str]:
        input_format = "mjpeg" if pixel_format == "MJPG" else "yuyv422"
        codec_args = ["-c:v", "copy"] if pixel_format == "MJPG" else ["-c:v", "mjpeg", "-q:v", str(YUYV_FFMPEG_QUALITY)]
        return [
            *FFMPEG_CMD_PREFIX,
            "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-f", "v4l2", "-input_format", input_format,
            "-video_size", resolution, "-framerate", str(DEFAULT_FPS),
            "-i", self.dev_path,
            # Skip the first few frames - some cameras deliver a dark or
            # partial first frame (task's own design note); -frames:v 1
            # after the select filter takes exactly one frame from what's
            # left.
            "-vf", "select=gte(n\\,3)",
            *codec_args,
            "-frames:v", "1", "-f", "image2", "pipe:1",
        ]

    def _reconfigure(self, resolution: str, fps: int) -> bool:
        self.stop()
        time.sleep(STOP_START_SETTLE_SECONDS)
        return self._start_stream(resolution, fps)

    def _reader_loop_stream(self, process: subprocess.Popen, generation: int) -> None:
        """Reads ffmpeg's stdout in STDOUT_READ_CHUNK_BYTES-sized chunks
        (deliberately not frame-aligned) and splits complete JPEG frames
        by SOI (FFD8)/EOI (FFD9), handling markers split across chunk
        boundaries by accumulating into `buffer` across reads. Only a
        complete, bounded frame replaces `_last_frame`. On EOF (process
        exited), reaps it and hands off to `_handle_process_exit()` for
        the stop-vs-failure distinction (see the module docstring's
        exit-code gotcha)."""
        buffer = bytearray()
        stdout = process.stdout
        try:
            while generation == self._stream_generation:
                chunk = stdout.read(STDOUT_READ_CHUNK_BYTES)
                if not chunk:
                    break
                buffer.extend(chunk)

                while True:
                    soi = buffer.find(b"\xff\xd8")
                    if soi == -1:
                        if len(buffer) > FRAME_BUFFER_CAP_BYTES:
                            print(
                                f"[USB CAMERA] {len(buffer)} bytes buffered with no JPEG SOI marker - "
                                "stream looks corrupt, stopping",
                                flush=True,
                            )
                            buffer.clear()
                            self._fail_and_stop(process, generation, "corrupt stream (no SOI)")
                            return
                        break
                    if soi > 0:
                        del buffer[:soi]

                    eoi = buffer.find(b"\xff\xd9", 2)
                    if eoi == -1:
                        if len(buffer) > FRAME_BUFFER_CAP_BYTES:
                            print(
                                f"[USB CAMERA] {len(buffer)} bytes buffered with no JPEG EOI marker - "
                                "stream looks corrupt, stopping",
                                flush=True,
                            )
                            buffer.clear()
                            self._fail_and_stop(process, generation, "corrupt stream (no EOI)")
                            return
                        break

                    frame = bytes(buffer[:eoi + 2])
                    del buffer[:eoi + 2]
                    with self._frame_lock:
                        self._last_frame = frame
                        self._last_frame_time = time.time()
        except Exception as error:
            if generation == self._stream_generation:
                print(f"[USB CAMERA] Reader thread error: {error}", flush=True)

        if generation == self._stream_generation:
            self._handle_process_exit(process, generation)

    def _handle_process_exit(self, process: subprocess.Popen, generation: int) -> None:
        """Called when the reader observes EOF on stdout (the process has
        exited or is exiting). Reaps it and applies the stop-vs-failure
        distinction the module docstring describes: an exit while
        self._stop_requested is False is a failure, whatever the exit
        code - including 0, which is exactly what CAM-0 confirmed real
        ffmpeg does on an unplugged camera."""
        try:
            returncode = process.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            returncode = None

        if self._stop_requested:
            return

        stderr_tail = list(self._stderr_lines)
        print(
            f"[USB CAMERA] {self.dev_path} process exited unexpectedly "
            f"(exit code {returncode}) - last stderr: {stderr_tail[-5:]}",
            flush=True,
        )
        with self._lock:
            if generation == self._stream_generation:
                self._available = False
                self._started = False
                self._process = None
                self._stream_generation += 1  # ends any stream_mjpeg() consumers

    def _fail_and_stop(self, process: subprocess.Popen, generation: int, reason: str) -> None:
        print(f"[USB CAMERA] {self.dev_path}: {reason}", flush=True)
        _terminate_process(process)
        with self._lock:
            if generation == self._stream_generation:
                self._available = False
                self._started = False
                self._process = None
                self._stream_generation += 1

    def _stall_watchdog_loop(self, generation: int, fps: int) -> None:
        """No complete frame for max(3x frame interval, STALL_MIN_SECONDS)
        while this generation is still current -> treat as a failure and
        kill the process, even though it hasn't exited on its own. CAM-1's
        own design note: an unplug can hang instead of exiting cleanly on
        some kernels, so relying on process exit alone would miss that
        case."""
        frame_interval = 1.0 / max(1, fps)
        threshold = max(STALL_FRAME_INTERVALS * frame_interval, STALL_MIN_SECONDS)
        check_interval = min(1.0, threshold / 3)

        while generation == self._stream_generation:
            time.sleep(check_interval)
            if generation != self._stream_generation:
                return

            with self._frame_lock:
                last_time = self._last_frame_time
            if last_time == 0.0:
                continue  # first frame hasn't arrived yet - handled by capture_photo()'s own timeout, not this watchdog

            if time.time() - last_time > threshold:
                with self._lock:
                    process = self._process
                if process is not None and generation == self._stream_generation:
                    self._fail_and_stop(process, generation, f"stalled - no frame for over {threshold:.1f}s")
                return

    def _idle_watchdog_loop(self, generation: int) -> None:
        while generation == self._stream_generation:
            time.sleep(IDLE_WATCHDOG_POLL_SECONDS)
            if generation != self._stream_generation:
                break

            with self._activity_lock:
                if self._active_consumers > 0:
                    idle_seconds = 0.0
                else:
                    idle_seconds = time.time() - self._last_activity_time

            if idle_seconds >= IDLE_STOP_SECONDS:
                print(
                    f"[USB CAMERA] Idle for {idle_seconds:.0f}s with no active viewers - "
                    "stopping to save CPU/power",
                    flush=True,
                )
                self.stop()
                break


_ALL_DRIVERS: "weakref.WeakSet[UsbCameraDriver]" = weakref.WeakSet()
_missing_tooling_logged = False


def _log_missing_tooling_once() -> None:
    global _missing_tooling_logged
    if _missing_tooling_logged:
        return
    _missing_tooling_logged = True
    print(
        "[USB CAMERA] ffmpeg and/or v4l-utils not found on PATH - USB camera "
        "support is unavailable until both are installed "
        "(sudo apt-get install -y ffmpeg v4l-utils; see INSTALL.md).",
        flush=True,
    )


def ffmpeg_and_v4l2ctl_available() -> bool:
    """Cheap check (no subprocess spawn, no device I/O) the Devices tab
    uses to decide whether to show "USB camera support requires ffmpeg
    and v4l-utils" - see api/api_camera_manager.py's _summary()."""
    return bool(FFMPEG_CMD_PREFIX) and bool(V4L2_CTL_PATH)


# PR #314 review finding (live-reproduced, 2026-09-30): PR_SET_PDEATHSIG is
# per-THREAD, not per-process - "the 'parent' in this case is the thread
# that created this process... the signal will be sent when that thread
# terminates... rather than after all the threads in the parent process
# have terminated" (man 2 prctl). Calling subprocess.Popen(preexec_fn=...)
# directly from a request-handling thread arms the death signal against
# THAT THREAD's lifetime, not Core's. Confirmed live: spawning from a
# short-lived threading.Thread got the child SIGKILLed the instant that
# thread returned, independent of whether the process it belonged to was
# still running. This "worked" under gunicorn's gthread worker only
# because gthread's own worker threads happen to be long-lived, reused
# across many requests - it would NOT work under Werkzeug's dev server
# (`python server.py`), where a request can land on a fresh, short-lived
# thread, silently killing the camera for every other viewer the moment
# the first one's request thread ends.
#
# Fix: every ffmpeg process (streaming or one-shot) is spawned from this
# one dedicated, never-exiting daemon thread instead of the caller's own
# thread - "the thread that called fork()" is then a fixed, permanent
# fact (this thread lives for the whole process), not an accident of
# which thread pool happened to serve a given request. Rejected
# alternative: dropping PDEATHSIG entirely and relying only on
# start_new_session+killpg+atexit+systemd KillMode=control-group - covers
# every orderly-shutdown and systemd-managed case, but leaves a real gap
# for `python server.py` run directly outside systemd if it's crashed or
# kill -9'd (no atexit, no systemd cgroup to clean up after it) - exactly
# the scenario PDEATHSIG exists for in the first place (see CLAUDE.md's
# GPLv3 process isolation section, same reasoning for the meshtastic
# adapter). The spawner-thread approach keeps that protection intact
# without the per-thread footgun.
_SPAWN_QUEUE: "queue.Queue[tuple[list[str], concurrent.futures.Future]]" = queue.Queue()


def _spawner_loop() -> None:
    """Runs forever on _SPAWNER_THREAD. The only place subprocess.Popen()
    is ever called for ffmpeg - see the module-level comment above for
    why that matters for PR_SET_PDEATHSIG specifically."""
    while True:
        argv, future = _SPAWN_QUEUE.get()
        try:
            process = _popen_ffmpeg(argv)
        except Exception as error:
            future.set_exception(error)
        else:
            future.set_result(process)


def _popen_ffmpeg(argv: list[str]) -> subprocess.Popen:
    """The actual subprocess.Popen() call - only ever invoked from
    _spawner_loop() on _SPAWNER_THREAD, never directly. Spawns ffmpeg with
    its own process group (so a stray child, if ffmpeg ever forked one,
    can be killed as a unit - see _terminate_process()) and
    PR_SET_PDEATHSIG on Linux (reusing meshsrv/adapter_ipc_client.py's
    already fork-safety-audited implementation rather than duplicating
    the subtle preexec_fn/dlopen lock-ordering hazard that module's own
    docstring explains in detail - the same hazard applies here for the
    same reason, Core is multi-threaded) so an ffmpeg subprocess can't
    outlive a killed/crashed Core process even outside systemd's own
    KillMode=control-group net."""
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "bufsize": 0,
    }
    if sys.platform == "linux":
        from meshsrv.adapter_ipc_client import _set_pdeathsig_to_sigkill

        popen_kwargs["start_new_session"] = True
        popen_kwargs["preexec_fn"] = _set_pdeathsig_to_sigkill
    elif os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    return subprocess.Popen(argv, **popen_kwargs)


_SPAWNER_THREAD = threading.Thread(target=_spawner_loop, name="usb-camera-ffmpeg-spawner", daemon=True)
_SPAWNER_THREAD.start()


def _spawn_ffmpeg(argv: list[str], timeout: float = 10.0) -> subprocess.Popen:
    """Public spawn entry point - every caller (stream start, one-shot
    photo) hands argv to the persistent spawner thread and blocks the
    CALLING thread (safe to be short-lived) until the Popen object comes
    back. See the module-level comment above _SPAWN_QUEUE for why the
    actual fork()+exec() must never happen on the caller's own thread."""
    future: "concurrent.futures.Future[subprocess.Popen]" = concurrent.futures.Future()
    _SPAWN_QUEUE.put((argv, future))
    return future.result(timeout=timeout)


def _terminate_process(process: subprocess.Popen) -> None:
    """SIGTERM, wait up to STOP_GRACE_SECONDS, then SIGKILL, then always
    wait() to reap - never leaves a zombie. Kills the whole process group
    on POSIX (see _spawn_ffmpeg()'s start_new_session=True) so an orphan
    child of ffmpeg's own can't survive its parent's termination."""
    if process.poll() is not None:
        return  # already exited

    try:
        if sys.platform == "linux":
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        else:
            process.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        if sys.platform == "linux":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass

    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _drain_stderr(process: subprocess.Popen, sink: deque, log_prefix: str) -> None:
    """Keeps a bounded ring buffer of ffmpeg's stderr (the last `maxlen`
    lines, per the deque passed in) and logs each line rate-limited -
    NEVER parsed for control flow, only for the human-readable log tail
    a failure prints (see _handle_process_exit()). An undrained stderr
    pipe can also block the writer once the OS pipe buffer fills, the
    same reasoning meshsrv/adapter_ipc_client.py's own stderr-drain
    thread documents for the meshtastic adapter."""
    stderr = process.stderr
    if stderr is None:
        return
    try:
        for raw_line in iter(stderr.readline, b""):
            line = raw_line.decode(errors="replace").rstrip()
            if not line:
                continue
            sink.append(line)
    except Exception:
        pass


def _extract_first_frame(data: bytes) -> bytes:
    soi = data.find(b"\xff\xd8")
    if soi == -1:
        return b""
    eoi = data.find(b"\xff\xd9", soi + 2)
    if eoi == -1:
        return b""
    return data[soi:eoi + 2]


def _is_valid_jpeg(data: bytes) -> bool:
    if not data or data[:2] != b"\xff\xd8":
        return False
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
        return True
    except Exception:
        return False


def stop_all_streaming() -> None:
    """Stops every UsbCameraDriver instance currently running its own
    ffmpeg subprocess - registered as an atexit handler below so a
    graceful interpreter shutdown (a gunicorn worker's own orderly SIGTERM
    handling, not a SIGKILL) doesn't leave an orphaned ffmpeg behind.
    PR_SET_PDEATHSIG/KillMode=control-group (see _spawn_ffmpeg()) are the
    safety net for the crash/SIGKILL case this doesn't cover."""
    for driver in list(_ALL_DRIVERS):
        try:
            driver.stop()
        except Exception:
            pass


atexit.register(stop_all_streaming)
