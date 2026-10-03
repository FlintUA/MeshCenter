"""H2-C Phase 2: serial hot-reconnect primitives - disconnect detection,
device-identity snapshotting, and by-id path resolution.

Pure, stdlib-only helpers (matching this package's convention - see
meshsrv/serial_port_supervisor.py's own module docstring) used by that
module's run_listener() and by server.py's radio_health_worker(). Kept
separate from SerialPortSupervisor itself because that class is
deliberately radio-protocol-agnostic ("this is about who currently owns
the OS-level serial device... not about the radio protocol itself" - see
its own docstring); the functions here know about device nodes and
Meshtastic CLI output shapes, which is a different, narrower concern
injected into that class via callables rather than imported into it.

Background: a live replug investigation (H2-C Phase 1, 2026-10-03) found
that an unplugged-then-replugged serial radio does not reliably recover
without a full service restart, for three distinct reasons this module's
functions help close:
  1. The listener subprocess can go hung-but-alive (the Meshtastic library
     detects and logs the disconnect internally but the Python process
     itself never exits) rather than crash-and-retry - invisible to
     anything that only watches the process's exit code.
  2. A replugged device can re-enumerate at a different /dev/ttyACMx path,
     which the listener's own fixed, once-resolved port string never
     notices.
  3. Nothing re-verifies the radio's identity when a port comes back, so
     a *different* physical radio appearing at the same path would be
     silently treated as the originally-accepted one.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

# The exact, distinctive phrase from the Meshtastic library's own
# stream_interface.py __reader() warning when the underlying device
# disappears mid-read - captured verbatim from a real journal line on dev
# (2026-10-03, see tests/test_serial_reconnect.py's fixture). Matched as a
# substring of the raw CLI line, not the whole line, since the library's
# own "WARNING file:... line:..." prefix formatting is not something this
# project controls or should depend on matching exactly.
DISCONNECT_LINE_MARKER = "device reports readiness to read but returned no data"


def line_signals_disconnect(line: str) -> bool:
    """True if a raw --listen stdout line is the Meshtastic library's own
    disconnect warning - the signal that lets run_listener() terminate a
    hung-but-alive listener process immediately instead of waiting
    indefinitely for a process exit that will never come."""
    return DISCONNECT_LINE_MARKER in (line or "")


@dataclass(frozen=True)
class DeviceIdentity:
    """A snapshot of the real device node backing a serial port path, at
    one point in time - enough to tell "the same physical device is still
    here" from "something else is here now at the same name" (a reused
    ttyACM number after a different device was plugged in, or a genuine
    radio swap)."""
    st_rdev: int
    st_ino: int
    st_ctime: float


def capture_device_identity(port: str) -> Optional[DeviceIdentity]:
    """Snapshot the device currently at `port` (resolving symlinks first,
    so a /dev/serial/by-id/* path and the /dev/ttyACMx it currently points
    at capture the same identity). None if nothing real is there right
    now - a missing path is not an error here, just "no identity yet"."""
    try:
        real = os.path.realpath(port)
        st = os.stat(real)
    except OSError:
        return None
    # st_rdev is POSIX-only (always present on Linux, including for a
    # plain file - 0 there) - getattr keeps this importable/testable on a
    # non-POSIX dev machine without changing real behavior on the actual
    # deployment target (Raspberry Pi).
    return DeviceIdentity(st_rdev=getattr(st, "st_rdev", 0), st_ino=st.st_ino, st_ctime=st.st_ctime)


def device_identity_changed(old: Optional[DeviceIdentity], port: str) -> bool:
    """True if the device at `port` is gone, or is a different device node
    than `old` - also True if `old` itself is None (nothing captured yet
    to compare against, so a caller deciding whether to re-verify should
    treat "unknown" the same as "changed", not assume it's fine)."""
    if old is None:
        return True
    current = capture_device_identity(port)
    if current is None:
        return True
    return current != old


DEFAULT_BY_ID_DIR = "/dev/serial/by-id"


def find_by_id_for_port(port: str, by_id_dir: str = DEFAULT_BY_ID_DIR) -> str:
    """The reverse of resolve_by_id_target(): given a real device path
    (e.g. /dev/ttyACM0), find the /dev/serial/by-id/* symlink that
    currently points at it, if any - used once at boot/accept time to
    persist a stable by-id reference into the connections model (see
    meshsrv/radio_connections.py), so a later replug can resolve forward
    through resolve_by_id_target() even if the ttyACMx number changes.
    Returns '' if the platform has no by-id directory (non-Linux, or a
    device with no udev by-id rule) or no link matches. `by_id_dir` is a
    parameter (not hardcoded inline) purely so tests can point it at a
    tmp_path fixture instead of the real /dev."""
    port = str(port or "").strip()
    if not port:
        return ""
    try:
        target = os.path.realpath(port)
    except OSError:
        return ""
    try:
        if not os.path.isdir(by_id_dir):
            return ""
        for name in sorted(os.listdir(by_id_dir)):
            candidate = os.path.join(by_id_dir, name)
            try:
                if os.path.realpath(candidate) == target:
                    return candidate
            except OSError:
                continue
    except OSError:
        return ""
    return ""


def resolve_by_id_target(by_id_path: str) -> str:
    """Resolve a /dev/serial/by-id/* symlink to its current real device
    path. Returns '' if `by_id_path` is blank or doesn't currently exist -
    callers fall back to single-candidate discovery in that case (see
    meshsrv/runtime_identity.py's discover_serial_ports())."""
    by_id_path = str(by_id_path or "").strip()
    if not by_id_path:
        return ""
    try:
        if not os.path.exists(by_id_path):
            return ""
        return os.path.realpath(by_id_path)
    except OSError:
        return ""


# Backoff schedule for retrying identity verification after a
# DETECTION_ERROR (the radio hasn't reappeared yet, or a probe failed
# transiently) - H2-C Phase 2 design: 5s, 10s, 30s, then settle at 60s.
# Deliberately NOT the schedule used for a plain crash-and-retry of an
# already-verified listener (SerialPortSupervisor's own existing fast
# backoff, unchanged) - this one is only for "waiting for the radio to
# come back and prove its identity before touching the port again", where
# --info is comparatively slow (seconds on a Pi Zero 2W) and shouldn't be
# re-run every couple of seconds.
IDENTITY_RETRY_BACKOFF_S = (5.0, 10.0, 30.0, 60.0)


def identity_retry_delay(attempt: int) -> float:
    """`attempt` is 0-indexed (0 = the first retry wait, right after the
    first DETECTION_ERROR). Settles at the schedule's last value (60s) for
    every attempt beyond its length, rather than growing unbounded."""
    index = min(max(attempt, 0), len(IDENTITY_RETRY_BACKOFF_S) - 1)
    return IDENTITY_RETRY_BACKOFF_S[index]
