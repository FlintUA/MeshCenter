"""Common lifecycle every peripheral device driver must implement.

Intentionally generic - not camera-specific - so a future non-camera
peripheral can share this base without redesign. See
camera/camera_driver.py for the camera-specific extension
(stream_mjpeg/capture_photo), and weather/weather_manager.py's
WeatherManager for the sibling registry shape this is meant to plug into
the same way (a dict of drivers keyed by stable id, one "active" at a
time).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DeviceDriver(ABC):
    # Stable id used as the registry key and as the persisted
    # active-driver value in devices.json, e.g. "csi" or
    # "usb:046d:09a4:video0". Must not change across restarts for the same
    # physical device, or a saved "active" selection will silently stop
    # resolving to anything.
    id: str

    # "camera" today; left open for future device kinds sharing this base.
    device_type: str

    display_name: str

    @abstractmethod
    def detect(self) -> dict[str, Any] | None:
        """Probe the hardware without starting it.

        Returns a metadata dict (model, vendor/product ids, capabilities -
        whatever is relevant for this device_type) if the device is
        physically present, or None if it isn't.
        """
        ...

    @abstractmethod
    def start(self, **options: Any) -> bool:
        """Start the device. Idempotent: calling start() while already
        started should succeed without side effects."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the device. Idempotent: calling stop() while already
        stopped should be a no-op, not an error."""
        ...

    @abstractmethod
    def get_status(self) -> dict[str, Any]:
        """Return at least {"ok": bool, "started": bool, "model": str},
        plus whatever additional fields this driver's device_type needs."""
        ...
