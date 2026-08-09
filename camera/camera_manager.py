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

csi registration is not wired in yet - build_camera_manager() only
discovers USB cameras today. dev/prod (CSI-only Pi Zero 2W, no USB port
free) intentionally aren't routed through this manager until
camera/csi_driver.py exists; only camtest (USB) exercises it for now.
See the project's usb-camera-plan notes for the full sequencing reason.
"""

from __future__ import annotations

from typing import Any, Iterator

from camera.camera_driver import CameraDriver
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

    csi registration will be added here once camera/csi_driver.py exists.
    """
    drivers: dict[str, CameraDriver] = {}
    for found in discover_usb_cameras():
        driver = UsbCameraDriver(found["dev_path"])
        drivers[driver.id] = driver

    return CameraManager(drivers, active_id=persisted_active_id)
