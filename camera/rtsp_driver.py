"""Network (RTSP) camera driver - CAM2 Phase 1a SKELETON.

Plugs a saved network source (camera/source_store.py) into the existing
CameraDriver / CameraManager framework. Phase 1a delivers the configuration,
secret handling, address policy and the registry integration; the upstream
ffmpeg process and the browser stream arrive in Phase 1b, so today
`stream_mjpeg()` yields nothing and `capture_photo()` returns no bytes.

Nothing here opens a network connection except `start()` re-validating the
address policy (which may resolve a host name): `detect()`, `get_status()` and
construction are free of network I/O, so listing cameras or rescanning never
touches the LAN.
"""
from __future__ import annotations

from typing import Any, Iterator

from camera import address_policy
from camera.camera_driver import CameraDriver


class RtspCameraDriver(CameraDriver):
    source_type = "rtsp"

    def __init__(self, source: dict, store, *, host_validator=None):
        self.id = str(source["id"])
        self.display_name = str(source.get("name") or "IP camera")
        self.config = {k: v for k, v in source.items() if k != "password"}
        self._store = store
        self._validate_host = host_validator or address_policy.validate_camera_host
        self._started = False
        self._state = "stopped"

    def _profile(self) -> str:
        return str(self.config.get("preferred_profile") or "sub")

    # DeviceDriver ----------------------------------------------------------

    def detect(self) -> dict[str, Any] | None:
        """Configuration only, no network I/O (rescan and the Devices tab call this)."""
        return {
            "model": self.display_name,
            "source_type": self.source_type,
            "profile": self._profile(),
        }

    def start(self, **_options: Any) -> bool:
        try:
            self._validate_host(str(self.config.get("host") or ""))
        except address_policy.AddressPolicyError:
            self._state = "error"
            return False
        self._started = True
        self._state = "idle"  # Phase 1b: becomes connecting/live/reconnecting
        return True

    def stop(self) -> None:
        self._started = False
        self._state = "stopped"

    def get_status(self) -> dict[str, Any]:
        return {
            "ok": self._state != "error",
            "started": self._started,
            "model": self.display_name,
            "source_type": self.source_type,
            "state": self._state,
            "profile": self._profile(),
            "codec": None,
            "resolution": None,
            "fps": None,
        }

    # CameraDriver ----------------------------------------------------------

    def stream_mjpeg(self) -> Iterator[bytes]:
        return iter(())  # Phase 1b

    def capture_photo(self, resolution: str | None = None) -> bytes:
        return b""  # Phase 1b


def drivers_from_store(store) -> list[RtspCameraDriver]:
    """One driver per saved source (reads the file, no network I/O)."""
    return [RtspCameraDriver(source, store) for source in store.list_sources()]
