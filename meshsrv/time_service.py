#!/usr/bin/env python3
"""System time status for MeshCenter.

Single source of truth for system time state (NTP sync, RTC status,
timezone) shared by the Time card, the e-paper System Screen and any future
schedule engine. Probes ``timedatectl``/``/etc/timezone`` on a background
thread and caches the result so request handlers never shell out directly.

RTC status (hardware/rtc_service.py's three independent stages - detected/
configured/readable) is folded in here too, but on its own, much longer
cache TTL (_RTC_CACHE_TTL) separate from the NTP/timezone one (_CACHE_TTL):
unlike NTP sync state, RTC hardware status essentially never changes on its
own between a config.txt edit + reboot (tracked separately by
hardware/hardware_config.py's pending-setup mechanism, not here), so
re-running i2cdetect/hwclock (real subprocess calls, up to 10s timeout
each) on every 20s tick would be pure waste.

is_trusted() deliberately still only looks at NTP synchronization, not RTC
- RTC here is a boot/offline-recovery time source, not a "trusted time"
signal for meshsrv/node_time_sync.py or meshsrv/schedule_engine.py.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from hardware import rtc_service

_lock: threading.Lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 20  # seconds

_rtc_cache: dict[str, Any] = {}
_rtc_cache_ts: float = 0.0
_RTC_CACHE_TTL = 300  # seconds - see module docstring


def _get_rtc_status() -> dict[str, Any]:
    """Cached wrapper around hardware.rtc_service.get_status() - refreshes
    at most once every _RTC_CACHE_TTL seconds. Never raises: rtc_service
    itself is documented not to, but this background thread must not die
    from it either way, so any unexpected exception is caught here too and
    treated as "RTC fully unavailable this cycle", not a crash."""
    global _rtc_cache, _rtc_cache_ts
    now = time.monotonic()
    if _rtc_cache and (now - _rtc_cache_ts) < _RTC_CACHE_TTL:
        return _rtc_cache

    try:
        status = rtc_service.get_status(model="ds3231")
    except Exception as error:
        print(f"[TIME] RTC probe failed: {error}", flush=True)
        status = {"ok": False}

    _rtc_cache = status
    _rtc_cache_ts = now
    return _rtc_cache


def _probe() -> dict[str, Any]:
    """Query the system for time status. Always returns a dict, never raises."""
    rtc_status = _get_rtc_status()
    stages = rtc_status.get("stages", {}) if rtc_status.get("ok") else {}
    rtc_detected = bool(stages.get("detected", {}).get("ok"))
    rtc_configured = bool(stages.get("configured", {}).get("ok"))
    rtc_readable = bool(stages.get("readable", {}).get("ok"))

    result: dict[str, Any] = {
        "synchronized": False,
        "source": "system",
        "quality": "system",
        "timezone": "UTC",
        # rtc_present kept for backward compatibility - same meaning as
        # rtc_configured ("the kernel has an RTC device configured"), not
        # the old raw Path("/sys/class/rtc/rtc0").exists() check.
        "rtc_present": rtc_configured,
        "rtc_detected": rtc_detected,
        "rtc_configured": rtc_configured,
        "rtc_readable": rtc_readable,
    }
    try:
        out = subprocess.check_output(
            [
                "timedatectl", "show",
                "-p", "NTPSynchronized",
                "-p", "Timesource",
                "-p", "Timezone",
            ],
            timeout=3, text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.strip().splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if key == "NTPSynchronized":
                result["synchronized"] = (value == "yes")
            elif key == "Timesource":
                result["source"] = value
            elif key == "Timezone":
                result["timezone"] = value
    except Exception as error:
        print(f"[TIME] timedatectl probe failed: {error}", flush=True)

    if result["timezone"] == "UTC":
        try:
            tz = Path("/etc/timezone").read_text().strip()
            if tz:
                result["timezone"] = tz
        except Exception:
            pass

    if result["synchronized"]:
        # NTP always wins, regardless of RTC state.
        result["quality"] = "synchronized"
        result["source"] = "ntp"
    elif result["rtc_configured"] and result["rtc_readable"]:
        # Configured-but-not-readable (e.g. permissions, or a different
        # device occupying /dev/rtc0 - see rtc_service.py's own docstring)
        # is deliberately NOT treated as quality "rtc" here anymore: the
        # old check only looked at /dev/rtc0 existing, which could report
        # "rtc" quality for an RTC that actually can't be read.
        result["quality"] = "rtc"
        result["source"] = "rtc"

    return result


def _refresh_cache() -> None:
    global _cache, _cache_ts
    data = _probe()
    with _lock:
        _cache = data
        _cache_ts = time.monotonic()


def _background_loop() -> None:
    """Refresh the cached status every ``_CACHE_TTL`` seconds, forever."""
    while True:
        try:
            _refresh_cache()
        except Exception as error:
            print(f"[TIME] Background refresh failed: {error}", flush=True)
        time.sleep(_CACHE_TTL)


def start_background_thread() -> None:
    """Start the caching background thread. Call once at Flask startup."""
    _refresh_cache()
    thread = threading.Thread(target=_background_loop, daemon=True, name="time-service")
    thread.start()


def get_status() -> dict[str, Any]:
    """Return the cached status. Never shells out - safe to call from request handlers."""
    with _lock:
        if not _cache:
            return {
                "utc": int(time.time()),
                "timezone": "UTC",
                "source": "system",
                "synchronized": False,
                "quality": "unknown",
                "rtc_present": False,
                "rtc_detected": False,
                "rtc_configured": False,
                "rtc_readable": False,
            }
        return {
            "utc": int(time.time()),
            "timezone": _cache.get("timezone", "UTC"),
            "source": _cache.get("source", "system"),
            "synchronized": _cache.get("synchronized", False),
            "quality": _cache.get("quality", "system"),
            "rtc_present": _cache.get("rtc_present", False),
            "rtc_detected": _cache.get("rtc_detected", False),
            "rtc_configured": _cache.get("rtc_configured", False),
            "rtc_readable": _cache.get("rtc_readable", False),
        }


def is_trusted() -> bool:
    """Quick check: can the system time currently be trusted?"""
    with _lock:
        return _cache.get("synchronized", False)
