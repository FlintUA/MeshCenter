"""Camera-specific extension of DeviceDriver.

Both csi_driver.py (Picamera2) and usb_driver.py (raw V4L2 via v4l2py)
implement this. The unifying contract is that every driver hands back
already-JPEG-encoded bytes, however it gets there internally:

- CSI decodes a raw sensor frame and re-encodes it via PIL (see
  camera/camera.py's existing capture_array()/fix_camera_colors() path,
  wrapped as-is by csi_driver.py).
- USB passes the camera's own native MJPEG straight through with zero
  decode/re-encode, since the confirmed hardware (Logitech QuickCam E
  3500) already produces MJPEG in hardware.

camera_manager.py therefore only ever deals in JPEG bytes, never a
driver-specific frame format - it owns the multipart/x-mixed-replace
boundary wrapping for /video_feed, drivers never build that themselves.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, Iterator

from devices.device_driver import DeviceDriver


class CameraDriver(DeviceDriver):
    device_type = "camera"

    @abstractmethod
    def stream_mjpeg(self) -> Iterator[bytes]:
        """Yield one JPEG-encoded frame at a time for as long as this
        driver is streaming. Starts the device if it isn't already
        running. The caller is responsible for the MJPEG multipart
        envelope around each yielded chunk."""
        ...

    @abstractmethod
    def capture_photo(self, resolution: str | None = None) -> bytes:
        """Return a single JPEG-encoded photo, optionally at a specific
        "WIDTHxHEIGHT" resolution."""
        ...

    def list_resolutions(self) -> list[str]:
        """"WIDTHxHEIGHT" strings this driver's device actually supports.
        Empty list means "resolution selection not offered for this
        driver" rather than "no resolutions" - callers should treat that
        as falling back to whatever default the driver already uses."""
        return []

    def get_controls(self) -> dict[str, Any]:
        """Current value of whatever image controls this driver exposes
        (brightness, contrast, white balance, ...). Shape is
        driver-specific - the Devices/Camera UI only renders what's
        actually present, it doesn't assume CSI's full control set exists
        on every driver."""
        return {}

    def set_controls(self, controls: dict[str, Any]) -> bool:
        """Apply a subset of get_controls()'s keys. Returns False if this
        driver doesn't support control changes at all."""
        return False
