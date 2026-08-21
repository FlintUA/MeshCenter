"""Read-only RTC status - three independent stages, never collapsed into a
single Online/Offline flag (per the project's own hardware spec):

  1. detected   - the RTC's I2C address answers on the bus (hardware.i2c_service)
  2. configured - the kernel has bound a Device Tree overlay for it, so
                   /dev/rtcN exists (dtoverlay=i2c-rtc,<model> took effect -
                   requires a reboot after being added to config.txt)
  3. readable   - `hwclock -r` can actually read the time off the device

These three can disagree in informative ways: detected-but-not-configured
means the overlay hasn't been added/hasn't survived a reboot yet;
configured-but-not-readable means the kernel bound *some* rtc0 (there can
only be one at a time) but this call can't read it (permissions, a
different RTC model already occupying it, etc).

Only DS3231 is a supported model today - RTC_MODELS is the extension point
for other RTC chips later, not a signal that this module is DS3231-specific
internally (the three-stage logic below doesn't hardcode 0x68 anywhere
except through RTC_MODELS).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from hardware import i2c_service

HWCLOCK_TIMEOUT = 10

# Extension point for future RTC chips (spec calls out DS1307/PCF8523/etc as
# possibilities) - only ds3231 is wired up end to end right now, matching
# scripts/meshcenter-hw-config's own whitelist.
RTC_MODELS = {
    "ds3231": {"address": "0x68", "display_name": "DS3231"},
}


def _stage(ok: bool, reason: str | None = None) -> dict:
    return {"ok": ok, "reason": reason}


def _find_rtc_device() -> str | None:
    """/dev/rtc0 is the canonical primary RTC once a Device Tree overlay
    binds one - checked directly first since that's the expected case.
    Falls back to scanning /sys/class/rtc for any rtcN in case rtc0 is
    occupied by something else (e.g. a USB RTC enumerated first)."""
    primary = Path("/dev/rtc0")
    if primary.exists():
        return str(primary)

    rtc_class_dir = Path("/sys/class/rtc")
    if rtc_class_dir.is_dir():
        for entry in sorted(rtc_class_dir.iterdir()):
            candidate = Path("/dev") / entry.name
            if candidate.exists():
                return str(candidate)

    return None


def _read_hwclock() -> dict:
    """Never raises. A garbled/unexpected hwclock output is reported as
    readable=True with the raw text in `raw_output` rather than parsed into
    a structured timestamp this module might get wrong - callers that need
    a real UTC value should use the OS clock (already synced from RTC at
    boot), not re-parse this text."""
    try:
        result = subprocess.run(
            ["hwclock", "-r"],
            capture_output=True,
            text=True,
            timeout=HWCLOCK_TIMEOUT,
        )
    except FileNotFoundError:
        return _stage(False, "hwclock not installed (util-linux-extra package missing)")
    except subprocess.TimeoutExpired:
        return _stage(False, f"hwclock timed out after {HWCLOCK_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001 - never raise out of this layer
        return _stage(False, f"hwclock failed: {exc}")

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return _stage(False, stderr or f"hwclock exited with code {result.returncode}")

    raw_output = (result.stdout or "").strip()
    stage = _stage(True)
    stage["raw_output"] = raw_output or None
    return stage


def get_status(model: str = "ds3231", bus: int = i2c_service.DEFAULT_BUS) -> dict:
    """Full three-stage RTC status for `model` on I2C bus `bus`."""
    model_info = RTC_MODELS.get(model)
    if model_info is None:
        return {
            "ok": False,
            "reason": f"unsupported RTC model: {model!r} (supported: {', '.join(sorted(RTC_MODELS))})",
        }

    address = model_info["address"]
    scan = i2c_service.scan_bus(bus)
    if scan.get("ok"):
        detected = _stage(address in scan.get("addresses", []))
    else:
        detected = _stage(False, scan.get("reason"))

    linux_device = _find_rtc_device()
    configured = _stage(linux_device is not None)

    readable = _read_hwclock() if configured["ok"] else _stage(False, "kernel has not configured an RTC device yet")

    return {
        "ok": True,
        "model": model,
        "display_name": model_info["display_name"],
        "interface": "i2c",
        "bus": bus,
        "address": address,
        "stages": {
            "detected": detected,
            "configured": configured,
            "readable": readable,
        },
        "linux_device": linux_device,
    }
