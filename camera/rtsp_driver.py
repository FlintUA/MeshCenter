"""Network (RTSP) camera driver (CAM2 Phase 1b).

Plugs a saved network source (camera/source_store.py) into the existing
CameraDriver / CameraManager framework. The stream itself lives in
camera/rtsp_stream.py (one ffmpeg per source, fMP4 fan-out); this class is the
thin adapter: lifecycle, status, viewer slots, and the MJPEG fallback.

Nothing here opens a network connection until a viewer asks for the stream:
construction, `detect()`, `get_status()` and `start()` never touch the LAN
(`start()` only re-checks the address policy, which may resolve a host name),
so listing cameras, rescanning or selecting a source never costs the camera a
session.
"""
from __future__ import annotations

import time
from typing import Any, Iterator

from camera import address_policy, mjpeg_fallback, rtsp_stream
from meshsrv import runtime_env
from camera.camera_driver import CameraDriver

DEFAULT_READY_WAIT_S = 6.0
# A viewer that has received no data for this long is ended. A WSGI generator only learns that its
# client went away when a WRITE fails; while the camera is down nothing is written, so without this
# an abandoned request (page reload, closed tab) would hold its viewer slot - and a server thread -
# until the camera came back. The browser player reconnects on its own.
VIEWER_IDLE_S = 15.0
# A stream request is authorised once, when it starts. MeshCenter's login is a signed cookie with no
# server-side session, so a logout (or a password change) cannot reach a response that is already
# open. Ending every stream after this long forces the player to reconnect, and that request is
# authorised again: a logged-out client keeps its stream for at most this long. The reconnect joins
# the warm upstream (about a second of interruption).
STREAM_MAX_AGE_S = 600.0


class ViewerLimitReached(Exception):
    pass


class StreamNotReady(Exception):
    def __init__(self, state: str, error_code: str = ""):
        super().__init__(state)
        self.state = state
        self.error_code = error_code


class Fmp4Stream:
    """Iterator over one viewer's fMP4 bytes. close() (called by the WSGI server when the client
    goes away, even before the first read) releases the viewer slot."""

    def __init__(self, driver: "RtspCameraDriver", cursor: rtsp_stream.Cursor, mse_codec: str):
        self._driver = driver
        self._cursor = cursor
        self._queue: list[bytes] = []
        self._open = True
        self._last_data = time.monotonic()
        self._opened_at = time.monotonic()
        self.mse_codec = mse_codec

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        relay = self._driver.upstream.relay
        while self._open:
            if time.monotonic() - self._opened_at > STREAM_MAX_AGE_S:
                self.close()
                break
            if self._queue:
                return self._queue.pop(0)
            chunks = relay.read(self._cursor, min(5.0, VIEWER_IDLE_S))
            if chunks is None:
                self.close()
                break
            self._driver.upstream.note_viewer_activity()
            if chunks:
                self._last_data = time.monotonic()
            elif time.monotonic() - self._last_data > VIEWER_IDLE_S:
                self.close()
                break
            self._queue.extend(chunks)
        raise StopIteration

    def close(self) -> None:
        if self._open:
            self._open = False
            self._driver.limiter.release(self._driver.id)


class RtspCameraDriver(CameraDriver):
    source_type = "rtsp"

    def __init__(self, source: dict, store, *, host_validator=None, limiter=None, upstream=None):
        self.id = str(source["id"])
        self.display_name = str(source.get("name") or "IP camera")
        self.config = {k: v for k, v in source.items() if k != "password"}
        self._store = store
        self._validate_host = host_validator or address_policy.validate_camera_host
        self.limiter = limiter or rtsp_stream.VIEWER_LIMITER
        self.upstream = upstream or rtsp_stream.RtspUpstream(
            self.config, lambda: store.get_password(self.id),
            host_validator=self._validate_host, limiter=self.limiter,
        )
        self._mjpeg = mjpeg_fallback.MjpegFromRelay(self.upstream)
        self._started = False
        self._address_error = False
        self._control_cache: tuple[float, dict] = (-1e9, {})

    # DeviceDriver ----------------------------------------------------------

    def detect(self) -> dict[str, Any] | None:
        """Configuration only, no network I/O (rescan and the Devices tab call this)."""
        return {"model": self.display_name, "source_type": self.source_type,
                "profile": self.upstream.profile}

    def start(self, **_options: Any) -> bool:
        try:
            self._validate_host(str(self.config.get("host") or ""))
        except address_policy.AddressPolicyError:
            self._address_error = True
            return False
        self._address_error = False
        self._started = True
        return True

    def stop(self) -> None:
        self._started = False
        self._mjpeg.stop()
        self.upstream.stop()

    def get_status(self) -> dict[str, Any]:
        up = self.upstream.status()
        state = "address_blocked" if self._address_error else up["state"]
        if state == "stopped" and self._started:
            state = "idle"  # selected, nothing connected yet: the upstream starts with the first viewer
        reason = mjpeg_fallback.fallback_disabled_reason()
        control = self._control_summary()
        return {
            "ok": state not in ("address_blocked", "ffmpeg_missing"),
            "started": self._started,
            "model": self.display_name,
            "source_type": self.source_type,
            "mjpeg_fallback": "disabled_low_power" if reason else "available",
            "container": runtime_env.is_container(),
            # camera-side control (Phase 2): what a probe found, no network I/O here
            "control_type": control["type"],
            "snapshot_fallback": control["type"] == "reolink" and bool(control["capabilities"].get("snapshot")),
            "zoom_control": control["type"] == "reolink" and bool(control["capabilities"].get("zoom")),
            **up,
            "state": state,
        }

    def _control_summary(self) -> dict:
        """The saved control section, re-read at most every 3 s (a probe changes it without restarting the
        stream, so the driver's start-time copy of the source would be stale). File I/O only."""
        at, cached = self._control_cache
        if time.monotonic() - at < 3.0:
            return cached
        try:
            control = self._store.get_source(self.id).get("control") or {}
        except Exception:
            control = {}
        summary = {"type": control.get("type") or "none", "capabilities": control.get("capabilities") or {}}
        self._control_cache = (time.monotonic(), summary)
        return summary

    # network-camera specifics ------------------------------------------------

    def open_fmp4(self, wait_s: float | None = None) -> Fmp4Stream:
        """Claim a viewer slot, make sure the upstream runs and wait for its init segment."""
        if not self.limiter.acquire(self.id):
            raise ViewerLimitReached()
        try:
            self._started = True
            self.upstream.ensure_running()
            timeout = wait_s if wait_s is not None else float(self.config.get("connect_timeout_s") or 10) + DEFAULT_READY_WAIT_S
            if not self.upstream.wait_ready(timeout):
                status = self.upstream.status()
                raise StreamNotReady(status["state"], status["error_code"])
            return Fmp4Stream(self, self.upstream.relay.open_cursor(), self.upstream.status()["mse_codec"] or "")
        except BaseException:
            self.limiter.release(self.id)
            raise

    def set_profile(self, profile: str) -> None:
        self.upstream.set_profile(profile)

    def reconnect(self) -> None:
        self.upstream.reconnect()

    # CameraDriver ----------------------------------------------------------

    def stream_mjpeg(self) -> Iterator[bytes]:
        """Fallback for browsers without MSE: JPEG frames transcoded from the shared upstream
        (never a second camera session; off on low-power hosts)."""
        if mjpeg_fallback.fallback_disabled_reason():
            return
        if not self.limiter.acquire(self.id):
            return
        try:
            self._started = True
            self.upstream.ensure_running()
            yield from self._mjpeg.frames()
        finally:
            self.limiter.release(self.id)

    def capture_photo(self, resolution: str | None = None) -> bytes:
        return b""  # Phase 3 (snapshots into the gallery)


def drivers_from_store(store) -> list[RtspCameraDriver]:
    """One driver per saved source (reads the file, no network I/O)."""
    return [RtspCameraDriver(source, store) for source in store.list_sources()]
