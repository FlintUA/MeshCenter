"""The only module allowed to invoke the privileged
scripts/meshcenter-hw-config helper (via `sudo -n`) or manage the
reboot-required pending-setup state that follows a successful enable-i2c/
configure-rtc call. Nothing else in the codebase should shell out to that
helper directly or touch data/hardware_pending.json - go through the
functions here instead.

Reboot-required actions (enabling I2C via a Device Tree parameter, adding
an RTC overlay) only take effect after the kernel re-reads config.txt on
the next boot. Between "the helper edited config.txt successfully" and
"a reboot has actually happened and the change took effect", this module
tracks a pending-setup record in an instance-scoped JSON file (not
per-profile - which physical Pi this is running on has nothing to do with
which Meshtastic radio profile is active) so the UI can tell the user
"reboot required" without them needing to remember what they clicked
before rebooting.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from hardware import i2c_service, rtc_service
from storage.json_store import safe_read_json, safe_write_json

HELPER_PATH = "/usr/local/sbin/meshcenter-hw-config"
HELPER_TIMEOUT = 15


def _run_helper(*args: str) -> dict:
    """Never raises. `sudo -n` (no interactive prompt) means a missing/
    misconfigured sudoers rule fails immediately and distinctly rather than
    hanging the request waiting on a password prompt nobody can answer."""
    try:
        result = subprocess.run(
            ["sudo", "-n", HELPER_PATH, *args],
            capture_output=True,
            text=True,
            timeout=HELPER_TIMEOUT,
        )
    except FileNotFoundError:
        return {"ok": False, "reason": "sudo is not available on this system"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": f"meshcenter-hw-config timed out after {HELPER_TIMEOUT}s"}
    except Exception as exc:  # noqa: BLE001 - never raise out of this layer
        return {"ok": False, "reason": f"meshcenter-hw-config failed: {exc}"}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        if "password is required" in stderr.lower() or "a password is required" in stderr.lower():
            reason = (
                "sudo is not configured for meshcenter-hw-config - install "
                "deploy/meshcenter-hw.sudoers (the installer does this automatically)"
            )
        else:
            reason = stderr or f"meshcenter-hw-config exited with code {result.returncode}"
        return {"ok": False, "reason": reason}

    return {"ok": True, "stdout": (result.stdout or "").strip()}


def _pending_path(data_dir: str) -> str:
    return str(Path(data_dir) / "hardware_pending.json")


def _load_pending(data_dir: str) -> dict | None:
    data = safe_read_json(_pending_path(data_dir), default=None)
    return data if isinstance(data, dict) and data else None


def _save_pending(data_dir: str, action: str, **extra) -> None:
    record = {"action": action, "set_at": time.time(), **extra}
    safe_write_json(_pending_path(data_dir), record)


def _clear_pending(data_dir: str) -> None:
    safe_write_json(_pending_path(data_dir), {})


def _boot_time_unix() -> float | None:
    """Seconds-since-epoch the kernel booted, derived from /proc/uptime.
    None (not an exception) on anything that isn't Linux with /proc, e.g.
    a developer's Windows box running the test suite - callers treat that
    the same as "can't tell yet, leave pending alone"."""
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            uptime_seconds = float(fh.readline().split()[0])
        return time.time() - uptime_seconds
    except Exception:
        return None


def enable_i2c(data_dir: str) -> dict:
    result = _run_helper("enable-i2c")
    if result.get("ok"):
        _save_pending(data_dir, action="enable_i2c")
    return result


def configure_rtc(data_dir: str, model: str) -> dict:
    result = _run_helper("configure-rtc", model)
    if result.get("ok"):
        _save_pending(data_dir, action="configure_rtc", model=model)
    return result


def helper_status() -> dict:
    return _run_helper("status")


def get_pending(data_dir: str) -> dict | None:
    """Current pending-setup record, or None if nothing is pending. Does
    NOT reconcile - call reconcile_pending() first (e.g. once at startup)
    if you want a stale record auto-resolved before reading it."""
    return _load_pending(data_dir)


def reconcile_pending(data_dir: str, boot_time_unix: float | None = None) -> dict | None:
    """Checks a pending-setup record (if any) against reality and clears it
    once a reboot has actually happened since it was recorded. Idempotent -
    safe to call on every status poll and at every startup; a no-op once
    nothing is pending.

    boot_time_unix is only ever passed explicitly by tests (to simulate "a
    reboot happened" without one); production callers rely on the
    /proc/uptime-derived default.

    Returns None if there was nothing pending, otherwise a dict describing
    what happened: still waiting (no reboot detected yet), or resolved with
    a "confirmed" bool saying whether the expected state was actually
    reached.
    """
    pending = _load_pending(data_dir)
    if pending is None:
        return None

    boot_time = boot_time_unix if boot_time_unix is not None else _boot_time_unix()
    set_at = pending.get("set_at")
    if boot_time is None or not isinstance(set_at, (int, float)) or boot_time <= set_at:
        # No reboot detected since this was recorded (or we can't tell) -
        # leave the record in place, still waiting.
        return {**pending, "resolved": False}

    action = pending.get("action")
    if action == "enable_i2c":
        scan = i2c_service.scan_bus()
        confirmed = bool(scan.get("ok"))
    elif action == "configure_rtc":
        model = pending.get("model", "ds3231")
        status = rtc_service.get_status(model=model)
        confirmed = bool(status.get("ok") and status.get("stages", {}).get("configured", {}).get("ok"))
    else:
        confirmed = False

    _clear_pending(data_dir)
    return {**pending, "resolved": True, "confirmed": confirmed}
