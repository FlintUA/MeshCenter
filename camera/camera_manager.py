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

import re
from typing import Any, Iterator

from camera.camera_driver import CameraDriver
from camera.csi_driver import CsiCameraDriver
from camera.usb_driver import UsbCameraDriver, discover_usb_cameras

# Matches the "(vendor:product)" suffix uvcvideo puts in its V4L2 card
# name, e.g. "UVC Camera (046d:09a4)" - confirmed live on camtest that
# Picamera2's own reported Model string for a USB webcam preserves this
# verbatim (it's libcamera's uvcvideo pipeline handler just passing the
# driver's own card name through). Used to recognize when csi_driver.py
# and usb_driver.py would otherwise both claim the same physical device.
_USB_IDS_IN_MODEL_RE = re.compile(r"\(([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\)")


def _usb_ids_from_model(model: str) -> tuple[str, str] | None:
    match = _USB_IDS_IN_MODEL_RE.search(model or "")
    if not match:
        return None
    return match.group(1).lower(), match.group(2).lower()


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
        device_type, active flag, plus whatever detect() reports (model,
        vendor/product ids, ...). Used for the Devices tab's camera
        cards."""
        summaries = []
        for driver_id, driver in self._drivers.items():
            info = driver.detect() or {}
            summaries.append({
                "id": driver_id,
                "display_name": driver.display_name,
                "device_type": driver.device_type,
                "active": driver_id == self._active_id,
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
    dev/prod), Picamera2 can see a USB UVC webcam directly and
    csi_driver.py's detect() reports it with the same model string
    usb_driver.py's own detection would use. Registering both for the same
    physical device would mean two drivers fighting over the same
    /dev/videoN the moment either one opens it - see the project's
    usb-camera-plan notes for the live test that motivated this. Dev/prod
    and any future USB camera on hardware without uvcvideo support are
    unaffected: csi_driver.py's model won't carry USB ids there, so
    usb_driver.py's cameras register normally.
    """
    drivers: dict[str, CameraDriver] = {}
    csi_usb_ids: tuple[str, str] | None = None

    csi_driver = CsiCameraDriver()
    csi_info = csi_driver.detect()
    if csi_info is not None:
        drivers[csi_driver.id] = csi_driver
        csi_usb_ids = _usb_ids_from_model(str(csi_info.get("model", "")))

    for found in discover_usb_cameras():
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
        driver = UsbCameraDriver(found["dev_path"])
        drivers[driver.id] = driver

    return CameraManager(drivers, active_id=persisted_active_id)
