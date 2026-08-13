#!/usr/bin/env python3
"""System time status for MeshCenter.

Single source of truth for system time state (NTP sync, RTC presence,
timezone) shared by the Time card, the e-paper System Screen and any future
schedule engine. Probes ``timedatectl``/``/etc/timezone`` on a background
thread and caches the result so request handlers never shell out directly.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any

_lock: threading.Lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 20  # seconds


def _probe() -> dict[str, Any]:
    """Query the system for time status. Always returns a dict, never raises."""
    result: dict[str, Any] = {
        "synchronized": False,
        "source": "system",
        "quality": "system",
        "timezone": "UTC",
        "rtc_present": Path("/sys/class/rtc/rtc0").exists(),
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
        result["quality"] = "synchronized"
        result["source"] = "ntp"
    elif result["rtc_present"]:
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
            }
        return {
            "utc": int(time.time()),
            "timezone": _cache.get("timezone", "UTC"),
            "source": _cache.get("source", "system"),
            "synchronized": _cache.get("synchronized", False),
            "quality": _cache.get("quality", "system"),
        }


def is_trusted() -> bool:
    """Quick check: can the system time currently be trusted?"""
    with _lock:
        return _cache.get("synchronized", False)
