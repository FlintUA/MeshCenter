"""Registry + active-driver dispatcher for camera backends.

Mirrors weather/weather_manager.py's WeatherManager shape (see that
file's own docstring) - a dict of drivers keyed by stable id, one
"active" at a time. Structurally similar by design, no shared code.

Unlike weather providers (a fixed, known-in-advance set configured via
config.py), camera drivers are discovered at runtime - a USB camera may
or may not be plugged in, and which /dev/videoN it lands on varies. See
build_camera_manager() for the discovery step; CameraManager itself is a
plain in-memory dispatcher with no file I/O of its own, matching
WeatherManager - whoever calls set_active() also persists the choice
(see storage/device_manager.py's active_camera_id field), the same way
api_settings.py persists settings.json before calling
weather_manager.set_active().

Both csi_driver.py and usb_driver.py are registered by build_camera_manager()
now, but this manager still isn't wired into the live /video_feed route -
see the project's usb-camera-plan notes for why that cutover is a
separate, deliberately later step (it needs to happen for dev/prod and
camtest at once, not incrementally).
"""

from __future__ import annotations

from typing import Any, Iterator

from camera.camera_driver import CameraDriver
from camera.csi_driver import CsiCameraDriver
from camera.usb_driver import UsbCameraDriver, discover_usb_cameras


class CameraManager:
    def __init__(self, drivers: dict[str, CameraDriver], active_id: str | None = None):
        self._drivers = drivers
        self._active_id = active_id if active_id in drivers else next(iter(drivers), None)

    @property
    def active_id(self) -> str | None:
        return self._active_id

    def active(self) -> CameraDriver | None:
        if self._active_id is None:
            return None
        return self._drivers.get(self._active_id)

    def list_drivers(self) -> list[dict[str, Any]]:
        """One summary dict per registered driver - id, display_name,
        device_type, active flag, plus model/vendor/product info. Used
        for the Devices tab's camera cards.

        Uses get_status() (cheap, no device I/O) for the *active* driver
        and detect() (opens the device briefly to (re)confirm it's still
        there) for inactive ones. Calling detect() on the active driver
        too would be wrong for usb_driver.py specifically: it always does
        a fresh open()/close() regardless of whether the driver is
        already streaming via its background thread, which would then be
        fighting its own reader thread over the same /dev/videoN - the
        exact kind of contention this whole framework exists to avoid.
        """
        summaries = []
        for driver_id, driver in self._drivers.items():
            is_active = driver_id == self._active_id
            info = (driver.get_status() if is_active else driver.detect()) or {}
            summaries.append({
                "id": driver_id,
                "display_name": driver.display_name,
                "device_type": driver.device_type,
                "active": is_active,
                **info,
            })
        return summaries

    def set_active(self, driver_id: str) -> bool:
        """Stop the previously active driver (releasing its background
        thread/device, if it has one) and start the requested one.

        Returns False if driver_id isn't registered or the new driver
        fails to start - the previous driver stays stopped either way,
        matching how a failed switch_camera_mode() already leaves CSI
        stopped rather than silently reverting to the old one.
        """
        if driver_id not in self._drivers:
            return False
        if driver_id == self._active_id:
            return True

        previous = self.active()
        if previous is not None:
            # This is the actual point of having a manager at all: the
            # deactivated driver's stop() releases its background thread
            # and device (see UsbCameraDriver.stop()) instead of it being
            # left running idle in the background.
            previous.stop()

        self._active_id = driver_id
        return self._drivers[driver_id].start()

    def get_status(self) -> dict[str, Any]:
        driver = self.active()
        if driver is None:
            return {"ok": False, "started": False, "model": "", "active_id": None}
        status = driver.get_status()
        status["active_id"] = self._active_id
        return status

    def stream_mjpeg(self) -> Iterator[bytes]:
        driver = self.active()
        if driver is None:
            return
        yield from driver.stream_mjpeg()

    def mjpeg_multipart_stream(self) -> Iterator[bytes]:
        """Wraps stream_mjpeg()'s raw JPEG frames in the
        multipart/x-mixed-replace envelope /video_feed needs - drivers
        themselves only ever hand back raw JPEG bytes (see
        camera_driver.py's CameraDriver contract), this is the one place
        that boundary format is built, same layout camera.py's own
        generate_mjpeg_stream() used before the cutover to this manager."""
        for frame in self.stream_mjpeg():
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Cache-Control: no-cache, no-store, must-revalidate\r\n"
                b"Pragma: no-cache\r\n"
                b"Expires: 0\r\n\r\n"
                + frame
                + b"\r\n"
            )

    def capture_photo(self, resolution: str | None = None) -> bytes:
        driver = self.active()
        if driver is None:
            return b""
        return driver.capture_photo(resolution)

    def list_resolutions(self) -> list[str]:
        driver = self.active()
        if driver is None:
            return []
        return driver.list_resolutions()


def build_camera_manager(persisted_active_id: str | None = None) -> CameraManager:
    """Discover available camera drivers and wrap them in a CameraManager.

    CSI is registered unconditionally (its detect() reports whether a
    sensor is actually present, same as any other driver) - unlike USB,
    there's nothing to enumerate for it, since camera.py always talks to
    "the" CSI port. Still not wired into the live /video_feed route yet
    (see the project's usb-camera-plan notes) - only exercised directly
    for now, the same way usb_driver.py was before this.

    Deduplicates by USB vendor:product id: on hardware where libcamera has
    a uvcvideo pipeline handler (confirmed live on camtest, NOT present on
    dev/prod), Picamera2 can see a USB UVC webcam directly - registering it
    as both "csi" and its own usb_driver.py entry would mean two drivers
    fighting over the same /dev/videoN the moment either one opens it. The
    vendor:product id comes from csi_driver.py's detect() parsing
    Picamera2's own Id string (see csi_driver.py's _detect_usb_ids() - the
    human-readable Model string turned out NOT to reliably carry the
    vendor:product id: confirmed live on a Pi 4B+ with two UVC webcams that
    one camera's Model has it and the other's doesn't, depending on
    whether that specific camera has its own USB product-string
    descriptor). Dev/prod and any future USB camera on hardware without
    uvcvideo support are unaffected: csi_driver.py reports no USB ids
    there, so usb_driver.py's cameras register normally.

    Visibility to Picamera2 alone isn't enough to trust the "csi" slot,
    though: confirmed live that libcamera's uvcvideo pipeline handler has
    no actual ISP/format-conversion behind it (unlike bcm2835-isp for real
    CSI sensors) - for a camera that only offers YUYV (no MJPEG to
    decode), Picamera2 silently returns the raw 2-channel YUYV buffer
    regardless of what output format is requested, which camera.py can't
    use. So a masquerading "csi" camera is only registered there when it
    actually has MJPEG (usb_driver.py's own "formats" field from
    discover_usb_cameras() is the source of truth for that, not the fact
    that Picamera2 happens to see it) - otherwise it's left for
    usb_driver.py, which has its own YUYV->JPEG software path (see that
    module's docstring).
    """
    drivers: dict[str, CameraDriver] = {}
    csi_usb_ids: tuple[str, str] | None = None

    usb_found = discover_usb_cameras()
    usb_formats_by_ids = {
        (str(found.get("vendor_id") or "").lower(), str(found.get("product_id") or "").lower()):
            found.get("formats") or set()
        for found in usb_found
    }

    csi_driver = CsiCameraDriver()
    csi_info = csi_driver.detect()
    if csi_info is not None:
        ids = csi_info.get("usb_ids")
        csi_functional = True
        if ids is not None:
            csi_functional = "MJPEG" in usb_formats_by_ids.get(ids, set())
            if not csi_functional:
                # detect() just opened Picamera2 to read the model/probe
                # usb_ids (camera_module.init_camera(), inside
                # csi_driver.py) and never closes it on its own - if this
                # driver isn't going to be registered/used, that handle
                # would otherwise linger indefinitely and fight
                # usb_driver.py for the same physical device on every
                # subsequent start()/rescan. Confirmed live on camtest:
                # without this, /video_feed failed with "[Errno 16]
                # Device or resource busy" on every attempt after a
                # rescan, recoverable only by explicitly releasing this
                # handle - not a real USB disconnect (lsusb/dmesg showed
                # the device fully present throughout). stop() is safe to
                # call on a never-started driver - it's csi_driver.py's
                # own close_camera(), which no-ops cleanly if picam2 is
                # already None.
                csi_driver.stop()
                print(
                    f"[CAMERA MANAGER] Not using the csi slot for {ids[0]}:{ids[1]} - "
                    "Picamera2 sees it via uvcvideo but it has no MJPEG, and "
                    "libcamera's software format conversion doesn't work for "
                    "YUYV-only cameras on this build (confirmed live: RGB888/"
                    "BGR888/YUV420/XRGB8888 all come back as raw 2-channel YUYV "
                    "data). Registering it as its own usb_driver.py entry "
                    "instead, which has its own YUYV->JPEG software path.",
                    flush=True,
                )
        if csi_functional:
            drivers[csi_driver.id] = csi_driver
            csi_usb_ids = ids

    for found in usb_found:
        vendor_id = str(found.get("vendor_id") or "").lower()
        product_id = str(found.get("product_id") or "").lower()
        if vendor_id and product_id and csi_usb_ids == (vendor_id, product_id):
            print(
                f"[CAMERA MANAGER] Not registering a separate usb_driver "
                f"entry for {vendor_id}:{product_id} - Picamera2 already "
                "has it via libcamera's own uvcvideo pipeline handler; "
                "registering both would fight over the same /dev/videoN.",
                flush=True,
            )
            continue
        driver = UsbCameraDriver(found["dev_path"], card_name=found.get("card"))
        drivers[driver.id] = driver

    return CameraManager(drivers, active_id=persisted_active_id)
