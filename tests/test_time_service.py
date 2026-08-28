"""Tests for meshsrv/time_service.py - the first test file for this module.

subprocess (timedatectl) and hardware.rtc_service.get_status() are mocked
throughout; nothing here touches real hardware. No server.py import
needed - this module only depends on hardware/rtc_service.py, which has
no hardware dependency of its own at import time either.

Module-level cache state (_cache/_cache_ts/_rtc_cache/_rtc_cache_ts,
_tz_last_logged) is reset around every test via an autouse fixture, same
pattern tests/test_cpu_history.py already uses for its own module-level
state.

Timezone-chain tests (P1 timezone-detection fix, MeshCenter_timezone_
detection_TZ_2026-08-28.md) never rely on the real filesystem of whatever
machine runs pytest - this bit the project once before (see the historical
comment inside test_rtc_service_exception_does_not_crash_probe below,
task 24: a test depended on the real /etc/timezone content of whichever
machine happened to run it). _mock_etc_timezone()/_mock_etc_localtime()
below always patch Path.read_text/.exists/.resolve explicitly, branching
on str(self) so only the path each test cares about is faked - real
Path instances for anything else keep working normally, and no test's
result can depend on whether the test runner is Windows (no /etc/* paths
at all) or Linux (real, possibly different, /etc/localtime).
"""

import subprocess
import time
from pathlib import Path

import pytest

import meshsrv.time_service as time_service


@pytest.fixture(autouse=True)
def _reset_time_service_state():
    def _reset():
        time_service._cache = {}
        time_service._cache_ts = 0.0
        time_service._rtc_cache = {}
        time_service._rtc_cache_ts = 0.0
        time_service._tz_last_logged = {}

    _reset()
    yield
    _reset()


def _mock_etc_timezone(monkeypatch, value: str | None) -> None:
    """value=None simulates /etc/timezone not existing (the common,
    non-error case on systems that don't use it, e.g. Droidian)."""
    def _read_text(self, *args, **kwargs):
        if self.as_posix() == "/etc/timezone" and value is not None:
            return value
        raise FileNotFoundError()

    monkeypatch.setattr(time_service.Path, "read_text", _read_text)


def _mock_etc_localtime(monkeypatch, *, exists: bool, resolved_target: str | None = None) -> None:
    """exists=False simulates no /etc/localtime at all. When exists=True,
    resolved_target is what Path.resolve() should return for it -
    resolved_target=None simulates /etc/localtime being a regular file
    (not a symlink), where resolve() just normalizes to the same path
    (doc section 18's container case)."""
    def _exists(self, *args, **kwargs):
        if self.as_posix() == "/etc/localtime":
            return exists
        return False

    def _resolve(self, *args, **kwargs):
        if self.as_posix() == "/etc/localtime":
            return Path(resolved_target) if resolved_target is not None else self
        return self

    monkeypatch.setattr(time_service.Path, "exists", _exists)
    monkeypatch.setattr(time_service.Path, "resolve", _resolve)


def _mock_no_file_fallbacks(monkeypatch) -> None:
    """Both /etc/timezone and /etc/localtime absent - isolates a test to
    whatever subprocess.check_output mock it sets up itself."""
    _mock_etc_timezone(monkeypatch, value=None)
    _mock_etc_localtime(monkeypatch, exists=False)


def _timedatectl_output(synchronized: bool, timesource: str | None = None, timezone: str = "UTC") -> str:
    # Timesource left unset by default when not synchronized, matching a
    # realistic `timedatectl show` on a system that isn't actually NTP-
    # synced right now (it does NOT report "Timesource=NTP" in that case) -
    # letting _probe()'s own "source" default of "system" stand unless a
    # later branch (synchronized / RTC-ready) overrides it.
    if timesource is None:
        timesource = "NTP" if synchronized else ""
    lines = [f"NTPSynchronized={'yes' if synchronized else 'no'}"]
    if timesource:
        lines.append(f"Timesource={timesource}")
    lines.append(f"Timezone={timezone}")
    return "\n".join(lines) + "\n"


def _rtc_status(detected=False, configured=False, readable=False, ok=True) -> dict:
    if not ok:
        return {"ok": False, "reason": "unsupported model"}
    return {
        "ok": True,
        "model": "ds3231",
        "stages": {
            "detected": {"ok": detected},
            "configured": {"ok": configured},
            "readable": {"ok": readable},
        },
    }


# ---------------- quality/source logic ----------------

def test_ntp_synchronized_wins_regardless_of_rtc_state(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=True))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=True),
    )

    result = time_service._probe()

    assert result["synchronized"] is True
    assert result["quality"] == "synchronized"
    assert result["source"] == "ntp"


def test_ntp_unsynchronized_rtc_fully_ready_reports_rtc_quality(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=True),
    )

    result = time_service._probe()

    assert result["synchronized"] is False
    assert result["quality"] == "rtc"
    assert result["source"] == "rtc"


def test_ntp_unsynchronized_rtc_configured_but_not_readable_falls_back_to_system(monkeypatch):
    # The tightened check from this task: configured-but-not-readable must
    # NOT be reported as quality "rtc" (the old code only checked
    # /dev/rtc0 existing, which this scenario would have wrongly passed).
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=False),
    )

    result = time_service._probe()

    assert result["quality"] == "system"
    assert result["source"] == "system"


def test_ntp_unsynchronized_rtc_not_detected_falls_back_to_system(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=False, configured=False, readable=False),
    )

    result = time_service._probe()

    assert result["quality"] == "system"
    assert result["source"] == "system"


# ---------------- new fields ----------------

def test_new_rtc_fields_present_and_match_stages(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=False),
    )

    result = time_service._probe()

    assert result["rtc_detected"] is True
    assert result["rtc_configured"] is True
    assert result["rtc_readable"] is False
    # Backward-compat field mirrors rtc_configured, not the old raw
    # /sys/class/rtc/rtc0 existence check.
    assert result["rtc_present"] is True


def test_get_status_exposes_new_fields_after_refresh(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=True),
    )

    time_service._refresh_cache()
    status = time_service.get_status()

    assert status["rtc_detected"] is True
    assert status["rtc_configured"] is True
    assert status["rtc_readable"] is True
    assert status["rtc_present"] is True
    assert status["quality"] == "rtc"


def test_get_status_defaults_when_cache_empty():
    status = time_service.get_status()
    assert status["rtc_detected"] is False
    assert status["rtc_configured"] is False
    assert status["rtc_readable"] is False
    assert status["rtc_present"] is False
    assert status["quality"] == "unknown"


# ---------------- error resilience ----------------

def test_rtc_service_exception_does_not_crash_probe(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))

    def _raise(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(time_service.rtc_service, "get_status", _raise)

    # Historical note (task 24): this test used to depend on the real
    # /etc/timezone content of whichever machine ran pytest, because the
    # old _probe() only fell back to reading it when the parsed timedatectl
    # timezone equaled "UTC" - which it did here, since _timedatectl_output()
    # defaults to timezone="UTC". That's now structurally impossible to hit
    # by accident: _timezone_from_timedatectl() finds "Timezone=UTC" in the
    # mocked subprocess output above, "UTC" validates fine via zoneinfo, and
    # detect_system_timezone() returns it immediately - /etc/timezone and
    # /etc/localtime are never even reached in this scenario, mocked or not.
    # The file-fallback chain itself (timedatectl failing, /etc/timezone
    # missing, /etc/localtime resolved) has its own dedicated tests below
    # (test_detect_system_timezone_*) that deliberately force each backend
    # to be reached, with real filesystem calls always mocked there.
    result = time_service._probe()  # must not raise

    assert result["rtc_detected"] is False
    assert result["rtc_configured"] is False
    assert result["rtc_readable"] is False
    assert result["quality"] == "system"
    # NTP/timezone side of the probe is unaffected by the RTC exception.
    assert result["synchronized"] is False
    assert result["timezone"] == "UTC"


def test_timedatectl_failure_does_not_crash_probe(monkeypatch):
    def _raise(*a, **k):
        raise subprocess.TimeoutExpired(cmd="timedatectl", timeout=3)

    monkeypatch.setattr(subprocess, "check_output", _raise)
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=True),
    )
    # timedatectl failing entirely means detect_system_timezone() falls
    # through to /etc/timezone then /etc/localtime - both explicitly
    # mocked absent here so the result ("UTC") never depends on the real
    # filesystem of whatever machine runs this test.
    _mock_no_file_fallbacks(monkeypatch)

    result = time_service._probe()  # must not raise

    assert result["synchronized"] is False
    # RTC side is unaffected by the timedatectl failure - still reports
    # quality "rtc" since NTP unsynced + RTC fully ready.
    assert result["quality"] == "rtc"
    assert result["timezone"] == "UTC"


# ---------------- RTC caching (_RTC_CACHE_TTL) ----------------

def test_rtc_status_not_requeried_within_ttl(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    calls = []

    def _tracked_get_status(**kwargs):
        calls.append(kwargs)
        return _rtc_status(detected=True, configured=True, readable=True)

    monkeypatch.setattr(time_service.rtc_service, "get_status", _tracked_get_status)

    time_service._probe()
    time_service._probe()

    assert len(calls) == 1


def test_rtc_status_requeried_after_ttl_expires(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    calls = []

    def _tracked_get_status(**kwargs):
        calls.append(kwargs)
        return _rtc_status(detected=True, configured=True, readable=True)

    monkeypatch.setattr(time_service.rtc_service, "get_status", _tracked_get_status)

    time_service._probe()
    assert len(calls) == 1

    # Simulate TTL having elapsed by directly backdating the module's own
    # cache timestamp - same "touch module state directly" approach the
    # rest of this test suite uses for module-level caches (see
    # tests/test_cpu_history.py), rather than mocking time.monotonic().
    time_service._rtc_cache_ts = time.monotonic() - time_service._RTC_CACHE_TTL - 1

    time_service._probe()
    assert len(calls) == 2


# ---------------- is_trusted() unaffected ----------------

def test_is_trusted_only_looks_at_ntp_synchronized(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=True, configured=True, readable=True),
    )

    time_service._refresh_cache()

    # RTC is fully ready here, but is_trusted() must still be False - it
    # only ever looks at NTP synchronization, per this task's explicit
    # instruction not to change that.
    assert time_service.is_trusted() is False

    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=True))
    time_service._refresh_cache()
    assert time_service.is_trusted() is True


# ---------------- detect_system_timezone() fallback chain ----------------
# Tests 1-5 below map directly to
# MeshCenter_timezone_detection_TZ_2026-08-28.md sections 17-18.

def test_detect_system_timezone_from_timedatectl(monkeypatch):
    # Test 1: timedatectl works.
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "Timezone=Europe/Berlin\n")
    _mock_no_file_fallbacks(monkeypatch)

    assert time_service.detect_system_timezone() == "Europe/Berlin"


def test_detect_system_timezone_falls_back_to_etc_timezone(monkeypatch):
    # Test 2: timedatectl inaccessible (Access denied, modeled as a non-zero
    # exit - see the module's own _log_once() docstring for why stderr is
    # never part of the exception text either way), /etc/timezone exists.
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "Timezone"])

    monkeypatch.setattr(subprocess, "check_output", _raise)
    _mock_etc_timezone(monkeypatch, value="Europe/Berlin\n")
    _mock_etc_localtime(monkeypatch, exists=False)

    assert time_service.detect_system_timezone() == "Europe/Berlin"


def test_detect_system_timezone_falls_back_to_etc_localtime(monkeypatch):
    # Test 3 - THE regression test for the reported Droidian bug:
    # timedatectl inaccessible, /etc/timezone absent, /etc/localtime
    # resolves into the zoneinfo database.
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "Timezone"])

    monkeypatch.setattr(subprocess, "check_output", _raise)
    _mock_etc_timezone(monkeypatch, value=None)
    _mock_etc_localtime(monkeypatch, exists=True, resolved_target="/usr/share/zoneinfo/Europe/Berlin")

    assert time_service.detect_system_timezone() == "Europe/Berlin"


def test_detect_system_timezone_all_sources_absent_falls_back_to_utc(monkeypatch):
    # Test 4: all sources absent.
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "Timezone"])

    monkeypatch.setattr(subprocess, "check_output", _raise)
    _mock_no_file_fallbacks(monkeypatch)

    assert time_service.detect_system_timezone() == "UTC"


def test_detect_system_timezone_invalid_name_skips_to_next_fallback(monkeypatch):
    # Test 5: timedatectl reports a name zoneinfo doesn't recognize - must
    # not be accepted as-is (requirement 4: validate before accepting), and
    # must not raise ZoneInfoNotFoundError outward. Falls through to
    # /etc/timezone, which has a genuinely valid name, proving this is a
    # real "skip to next fallback", not just "give up immediately".
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "Timezone=Europe/InvalidCity\n")
    _mock_etc_timezone(monkeypatch, value="Europe/Berlin\n")
    _mock_etc_localtime(monkeypatch, exists=False)

    assert time_service.detect_system_timezone() == "Europe/Berlin"


def test_detect_system_timezone_invalid_name_everywhere_falls_back_to_utc(monkeypatch):
    # Same as above, but no valid fallback exists anywhere in the chain -
    # must land on "UTC", not raise and not return the invalid name.
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: "Timezone=Not/AZone\n")
    _mock_etc_timezone(monkeypatch, value="Also/NotAZone\n")
    _mock_etc_localtime(monkeypatch, exists=True, resolved_target="/usr/share/zoneinfo/Still/NotAZone")

    assert time_service.detect_system_timezone() == "UTC"


def test_detect_system_timezone_localtime_regular_file_not_symlink_does_not_crash(monkeypatch):
    # Integration test (doc section 18, "Container"): /etc/localtime is a
    # regular file, not a symlink into /usr/share/zoneinfo. Path.resolve()
    # on a non-symlink just normalizes to the same path, so relative_to()
    # raises ValueError inside _timezone_from_localtime() - must be caught
    # there (it already is, by construction) and fall back to UTC, not
    # crash detect_system_timezone().
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "Timezone"])

    monkeypatch.setattr(subprocess, "check_output", _raise)
    _mock_etc_timezone(monkeypatch, value=None)
    # resolved_target=None means resolve() returns the same /etc/localtime
    # path unchanged - exactly what happens for a real, non-symlink file.
    _mock_etc_localtime(monkeypatch, exists=True, resolved_target=None)

    assert time_service.detect_system_timezone() == "UTC"


def test_detect_system_timezone_never_raises_when_a_backend_misbehaves(monkeypatch):
    # Belt-and-suspenders check for detect_system_timezone()'s own outer
    # try/except: even if a backend somehow raised past its own internal
    # try/except (a bug, not an expected path), the loop must still move
    # on to the next backend instead of propagating.
    def _timedatectl_raises_unexpectedly(*a, **k):
        raise RuntimeError("boom - should never happen, but must not crash detection")

    monkeypatch.setattr(time_service, "_timezone_from_timedatectl", _timedatectl_raises_unexpectedly)
    _mock_etc_timezone(monkeypatch, value="Europe/Berlin\n")
    _mock_etc_localtime(monkeypatch, exists=False)

    assert time_service.detect_system_timezone() == "Europe/Berlin"


# ---------------- DST (requires the `tzdata` package on non-Linux hosts -
# see requirements-dev.txt) ----------------

def test_dst_summer_offset_for_europe_berlin():
    # Test 6: DST summer - MeshCenter_timezone_detection_TZ_2026-08-28.md's
    # own example date/timezone.
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 8, 28, 14, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert dt.utcoffset() == timedelta(hours=2)  # CEST


def test_dst_winter_offset_for_europe_berlin():
    # Test 7: DST winter.
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    dt = datetime(2026, 12, 15, 14, 0, tzinfo=ZoneInfo("Europe/Berlin"))
    assert dt.utcoffset() == timedelta(hours=1)  # CET


# ---------------- log-dedup (Вариант A: log once until state changes) ----

def test_timedatectl_timezone_failure_logged_only_once(monkeypatch, capsys):
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "Timezone"])

    monkeypatch.setattr(subprocess, "check_output", _raise)
    _mock_no_file_fallbacks(monkeypatch)

    time_service.detect_system_timezone()
    time_service.detect_system_timezone()
    time_service.detect_system_timezone()

    out = capsys.readouterr().out
    assert out.count("[TIME] timedatectl unavailable, using fallback timezone detection") == 1


def test_timedatectl_timezone_failure_logs_again_when_the_error_changes(monkeypatch, capsys):
    # Same tag, different underlying failure - must log again, not stay
    # suppressed forever just because *some* error was already seen once.
    def _raise_denied(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "Timezone"])

    def _raise_missing_binary(*a, **k):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'timedatectl'")

    _mock_no_file_fallbacks(monkeypatch)

    monkeypatch.setattr(subprocess, "check_output", _raise_denied)
    time_service.detect_system_timezone()

    monkeypatch.setattr(subprocess, "check_output", _raise_missing_binary)
    time_service.detect_system_timezone()

    out = capsys.readouterr().out
    assert out.count("[TIME] timedatectl unavailable, using fallback timezone detection") == 2


def test_ntp_probe_failure_is_also_log_deduped(monkeypatch, capsys):
    # The task explicitly called out extending dedup to the pre-existing
    # NTP/Timesource probe (not just the new timezone code), so Droidian
    # doesn't keep flooding the journal on that side either.
    def _raise(*a, **k):
        raise subprocess.CalledProcessError(1, ["timedatectl", "show", "-p", "NTPSynchronized", "-p", "Timesource"])

    monkeypatch.setattr(subprocess, "check_output", _raise)

    time_service._probe_ntp_status()
    time_service._probe_ntp_status()

    out = capsys.readouterr().out
    assert out.count("timedatectl_ntp failed") == 1


# ---------------- get_status()/quality integration with the new chain ----

def test_probe_uses_detect_system_timezone_result(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: _timedatectl_output(synchronized=False, timezone="Europe/Berlin"))
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=False, configured=False, readable=False),
    )

    result = time_service._probe()

    assert result["timezone"] == "Europe/Berlin"


def test_probe_timezone_independent_of_ntp_probe_failure(monkeypatch):
    # A Droidian-shaped system: the NTP/Timesource probe fails, but
    # timezone detection (a separate subprocess call) still succeeds.
    def _check_output(cmd, *a, **k):
        if "-p" in cmd and "Timezone" in cmd and "NTPSynchronized" not in cmd:
            return "Timezone=Europe/Berlin\n"
        raise subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(subprocess, "check_output", _check_output)
    monkeypatch.setattr(
        time_service.rtc_service, "get_status",
        lambda **kwargs: _rtc_status(detected=False, configured=False, readable=False),
    )

    result = time_service._probe()

    assert result["timezone"] == "Europe/Berlin"
    assert result["synchronized"] is False
    assert result["source"] == "system"
