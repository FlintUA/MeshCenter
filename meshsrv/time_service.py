#!/usr/bin/env python3
"""System time status for MeshCenter.

Single source of truth for system time state (NTP sync, RTC status,
timezone) shared by the Time card, the e-paper System Screen and any future
schedule engine. Probes ``timedatectl``/``/etc/timezone``/``/etc/localtime``
on a background thread and caches the result so request handlers never
shell out directly.

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

Timezone detection (detect_system_timezone()) is a separate fallback chain
from NTP/Timesource probing (_probe_ntp_status()) - a system where
``timedatectl`` is entirely inaccessible (observed on Droidian: the whole
D-Bus query is denied, not one unsupported property) must not lose
timezone detection just because NTP status can't be read, and vice versa.
Each backend in the chain (timedatectl / /etc/timezone / /etc/localtime)
has its own try/except and returns ``None`` on any failure rather than
raising - see that function's own docstring for why. This module never
calls ``timedatectl set-timezone`` or otherwise mutates system
configuration - detection only.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from hardware import rtc_service

_lock: threading.Lock = threading.Lock()
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 20  # seconds

_rtc_cache: dict[str, Any] = {}
_rtc_cache_ts: float = 0.0
_RTC_CACHE_TTL = 300  # seconds - see module docstring

_ZONEINFO_ROOT = Path("/usr/share/zoneinfo")

# Log-dedup for the timezone/NTP probing below (P1 timezone-detection fix):
# maps a stable per-backend tag to the (exception type name, exception text)
# last printed for it, so the same recurring failure (e.g. timedatectl
# "Access denied" on every 20s tick) prints once, not on every _probe().
# Manual dict + print(), not the logging module - see module docstring and
# _log_once()'s own docstring for why comparing (type, text) is safe here.
_tz_last_logged: dict[str, tuple[str, str]] = {}


def _log_once(tag: str, error: Exception, message: str | None = None) -> None:
    """Print an informational line for `error` at most once per distinct
    (type, text) pair recorded under `tag` - avoids flooding the journal
    with the same expected failure every _CACHE_TTL seconds, while still
    logging again if the failure mode actually changes (a different error
    appears, or a since-cleared one recurs differently).

    Comparing (type(error).__name__, str(error)) rather than just str(error)
    guards against two different exception types that happen to stringify
    identically - cheap to be exact. Both halves are stable across repeated
    calls for the same underlying failure: every subprocess call in this
    module runs with stderr=DEVNULL, so no dynamic process output (which
    could vary between calls - a PID, a timestamp) ever reaches str(error);
    only the fixed argv/returncode/timeout values do (verified against
    subprocess.CalledProcessError/FileNotFoundError/TimeoutExpired's actual
    __str__ output - none include stderr content).
    """
    signature = (type(error).__name__, str(error))
    if _tz_last_logged.get(tag) == signature:
        return
    _tz_last_logged[tag] = signature
    print(message or f"[TIME] {tag} failed: {error}", flush=True)


def _timezone_from_timedatectl() -> str | None:
    """Query only the Timezone property, independent of NTP/Timesource -
    a system where timedatectl is entirely inaccessible (Droidian: the
    whole D-Bus query is denied) must not lose timezone detection just
    because the same probe also asked for other properties. Own try/except:
    never raises, returns None if the property can't be determined this way
    so detect_system_timezone() falls through to the next backend."""
    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "Timezone"],
            timeout=3, text=True, stderr=subprocess.DEVNULL,
        )
    except Exception as error:
        _log_once(
            "timedatectl_timezone", error,
            "[TIME] timedatectl unavailable, using fallback timezone detection",
        )
        return None

    for line in out.strip().splitlines():
        if line.startswith("Timezone="):
            value = line.split("=", 1)[1].strip()
            return value or None
    return None


def _timezone_from_etc_timezone() -> str | None:
    """Read /etc/timezone directly - present on Debian/Raspberry Pi OS/
    Ubuntu, absent on Droidian. Own try/except: a missing file is the
    expected, common case on systems that don't use it (no /etc/timezone
    is not itself an error), anything else is logged once."""
    try:
        value = Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except Exception as error:
        _log_once("etc_timezone", error)
        return None
    return value or None


def _timezone_from_localtime() -> str | None:
    """Resolve /etc/localtime and, if it points inside the system zoneinfo
    database, extract the IANA name from the relative path - e.g.
    /usr/share/zoneinfo/Europe/Berlin -> "Europe/Berlin". This is the
    fallback that actually fixes the reported Droidian bug: timedatectl is
    denied and /etc/timezone doesn't exist there, but /etc/localtime is
    still configured correctly.

    Handles the /etc/localtime-is-a-regular-file-not-a-symlink case
    (containers, some minimal images) without crashing: Path.resolve() on a
    non-symlink just normalizes the same absolute path, so relative_to()
    below raises ValueError (that path is not under _ZONEINFO_ROOT) and
    this returns None like any other undetectable case - no special
    handling needed, the existence check already covers it structurally.

    Own try/except: never raises, returns None (not "UTC" itself - that's
    detect_system_timezone()'s job) if this source is unusable.
    """
    try:
        localtime = Path("/etc/localtime")
        if not localtime.exists():
            return None
        resolved = localtime.resolve()
        try:
            relative = resolved.relative_to(_ZONEINFO_ROOT)
        except ValueError:
            return None
        # .as_posix(), not str(): IANA timezone names are always
        # forward-slash ("Europe/Berlin"), regardless of the host OS's own
        # path separator - str() would use backslashes on Windows, which
        # this code never actually runs on in production (Linux-only
        # /etc/localtime), but the distinction matters for tests that
        # exercise this function directly on a Windows dev machine.
        name = relative.as_posix()
        return name or None
    except Exception as error:
        _log_once("etc_localtime", error)
        return None


def _validate_timezone(name: str | None) -> str | None:
    """Accept `name` only if zoneinfo actually recognizes it as an IANA
    timezone - centralized here (not duplicated inside each
    _timezone_from_*() backend) so "is this I/O usable" and "is this name
    semantically valid" stay two separate concerns. An invalid name (typo,
    truncated path, non-tzdata string) returns None so the caller falls
    through to the next fallback instead of accepting garbage."""
    if not name:
        return None
    try:
        ZoneInfo(name)
    except Exception:
        return None
    return name


def detect_system_timezone() -> str:
    """Return an IANA timezone name (e.g. "Europe/Berlin"), never a fixed
    UTC offset - DST transitions must keep working automatically via
    zoneinfo, not a hardcoded +2/+1. Tries timedatectl, then
    /etc/timezone, then /etc/localtime, in that order; each candidate is
    validated (see _validate_timezone()) before being accepted, and "UTC"
    is only ever the last-resort return value, never assumed early.

    This function itself never raises: each backend already guards its own
    I/O, and the outer try/except here is a second net so a bug inside one
    backend can never take down timezone detection for the whole probe.
    """
    for backend in (_timezone_from_timedatectl, _timezone_from_etc_timezone, _timezone_from_localtime):
        try:
            candidate = backend()
        except Exception as error:
            _log_once(f"{backend.__name__}_unexpected", error)
            candidate = None

        validated = _validate_timezone(candidate)
        if validated:
            return validated

    return "UTC"


def _probe_ntp_status() -> dict[str, Any]:
    """Query NTPSynchronized/Timesource only - separate subprocess call from
    timezone detection (see detect_system_timezone()) so a timezone-only
    failure (or vice versa) never affects the other. Never raises."""
    result: dict[str, Any] = {"synchronized": False, "source": "system"}
    try:
        out = subprocess.check_output(
            ["timedatectl", "show", "-p", "NTPSynchronized", "-p", "Timesource"],
            timeout=3, text=True, stderr=subprocess.DEVNULL,
        )
    except Exception as error:
        _log_once("timedatectl_ntp", error)
        return result

    for line in out.strip().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if key == "NTPSynchronized":
            result["synchronized"] = (value == "yes")
        elif key == "Timesource" and value:
            result["source"] = value
    return result


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

    ntp_status = _probe_ntp_status()

    result: dict[str, Any] = {
        "synchronized": ntp_status["synchronized"],
        "source": ntp_status["source"],
        "quality": "system",
        "timezone": detect_system_timezone(),
        # rtc_present kept for backward compatibility - same meaning as
        # rtc_configured ("the kernel has an RTC device configured"), not
        # the old raw Path("/sys/class/rtc/rtc0").exists() check.
        "rtc_present": rtc_configured,
        "rtc_detected": rtc_detected,
        "rtc_configured": rtc_configured,
        "rtc_readable": rtc_readable,
    }

    if result["synchronized"]:
        # NTP always wins, regardless of RTC state.
        result["quality"] = "synchronized"
        result["source"] = "ntp"
    elif rtc_configured and rtc_readable:
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
