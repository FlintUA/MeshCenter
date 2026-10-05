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
    radio swap).

    Deliberately (st_rdev, st_ino) - NOT st_ctime (review follow-up). On
    Linux, a re-created devtmpfs node USUALLY gets a fresh st_ino on its
    own; st_ctime can also change for the SAME device on a plain metadata
    update (chmod/chown/ACL - udev rules, ModemManager, ...), which would
    have made terminate_if_device_changed() kill a perfectly healthy
    listener and force an unnecessary --info re-verification.

    Review round 2, item 1 - also usb_busnum/usb_devnum (both None if
    unavailable): st_ino alone is NOT reliable - live-caught on Linux CI,
    a freed inode number was reused immediately on a replug, so the "same"
    inode masked a genuine device change. USB enumeration (busnum/devnum
    from sysfs) is monotonic per physical plug-in (dmesg showed "device
    number 3 -> 4 -> 5" for the same radio replugged repeatedly during the
    H2-C Phase 1 live investigation) in a way inode reuse is not - it is
    the primary signal when available, with (st_rdev, st_ino) kept as the
    fallback for a non-Linux platform, a non-USB serial device, or a test
    double with no real sysfs to read."""
    st_rdev: int
    st_ino: int
    usb_busnum: Optional[int] = None
    usb_devnum: Optional[int] = None


# Linux-only, read via sysfs - see DeviceIdentity's own docstring for why
# this exists alongside (st_rdev, st_ino) rather than replacing them.
_SYSFS_TTY_CLASS_DIR = "/sys/class/tty"
# Bounds the walk up the sysfs device tree from a tty's own `device` link
# to the ancestor directory that actually has busnum/devnum files (the USB
# device node itself, not one of the intermediate interface/endpoint
# nodes) - generous for any real USB topology, just a safety net against
# an unexpected sysfs shape looping forever.
_USB_SYSFS_WALK_LIMIT = 8


def capture_usb_enumeration(port: str) -> tuple[Optional[int], Optional[int]]:
    """(busnum, devnum) ints for the USB device backing `port`, or
    (None, None) if unavailable - non-Linux, not a USB-serial device, or
    sysfs doesn't expose it for some other reason. A public, deliberately
    injectable seam: tests monkeypatch this function directly rather than
    faking the full /sys/class/tty walk and the real files underneath it,
    since actual sysfs/inode behavior is filesystem- and platform-
    dependent in ways a unit test must never rely on (review round 2,
    item 1 - exactly what the (st_rdev, st_ino)-only design got wrong)."""
    try:
        real = os.path.realpath(port)
        tty_name = os.path.basename(real)
        if not tty_name:
            return None, None
        current = os.path.realpath(os.path.join(_SYSFS_TTY_CLASS_DIR, tty_name, "device"))
        if not current or not os.path.isdir(current):
            return None, None
        for _ in range(_USB_SYSFS_WALK_LIMIT):
            busnum_path = os.path.join(current, "busnum")
            devnum_path = os.path.join(current, "devnum")
            if os.path.isfile(busnum_path) and os.path.isfile(devnum_path):
                with open(busnum_path, encoding="ascii") as f:
                    busnum = int(f.read().strip())
                with open(devnum_path, encoding="ascii") as f:
                    devnum = int(f.read().strip())
                return busnum, devnum
            parent = os.path.dirname(current)
            if not parent or parent == current:
                return None, None
            current = parent
    except (OSError, ValueError):
        return None, None
    return None, None


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
    usb_busnum, usb_devnum = capture_usb_enumeration(port)
    return DeviceIdentity(
        st_rdev=getattr(st, "st_rdev", 0),
        st_ino=st.st_ino,
        usb_busnum=usb_busnum,
        usb_devnum=usb_devnum,
    )


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


# Review round 4, item 1 (live-caught, 2026-10-04): a long outage used to
# grow identity_retry_delay() all the way to 60s WHILE THE DEVICE WAS STILL
# ABSENT - so a replug right after that backoff escalated could still wait
# up to 60s before the first --info probe even ran, even though the only
# thing that happened during the absence was "keep checking os.path.exists()
# got nothing". DEVICE_PRESENCE_POLL_INTERVAL_S is for exactly that
# "nothing there yet" case: cheap (a single os.path.exists()/by-id-resolve,
# no CLI subprocess), fixed-interval, no growing backoff - so replug->MATCH
# latency is dominated by how long the device itself takes to actually
# re-enumerate and settle, not by a stale backoff counter from before it
# came back.
DEVICE_PRESENCE_POLL_INTERVAL_S = 2.0

# Backoff schedule for retrying identity verification after a
# DETECTION_ERROR (the radio IS present but a real --info probe against it
# failed/errored) - H2-C Phase 2 design: 5s, 10s, 30s, then settle at 60s.
# Deliberately NOT the schedule used for a plain crash-and-retry of an
# already-verified listener (SerialPortSupervisor's own existing fast
# backoff, unchanged) - this one is only for "the device is here, but
# couldn't be verified yet", where --info is comparatively slow (seconds on
# a Pi Zero 2W) and shouldn't be re-run every couple of seconds. Reset to
# attempt 0 whenever the device disappears again (see
# DEVICE_PRESENCE_POLL_INTERVAL_S above) - this schedule is scoped to
# "device present, probe failing", not "device absent".
IDENTITY_RETRY_BACKOFF_S = (5.0, 10.0, 30.0, 60.0)


def identity_retry_delay(attempt: int) -> float:
    """`attempt` is 0-indexed (0 = the first retry wait, right after the
    first DETECTION_ERROR). Settles at the schedule's last value (60s) for
    every attempt beyond its length, rather than growing unbounded."""
    index = min(max(attempt, 0), len(IDENTITY_RETRY_BACKOFF_S) - 1)
    return IDENTITY_RETRY_BACKOFF_S[index]
