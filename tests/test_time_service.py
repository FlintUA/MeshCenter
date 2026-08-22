"""Tests for meshsrv/time_service.py - the first test file for this module.

subprocess (timedatectl) and hardware.rtc_service.get_status() are mocked
throughout; nothing here touches real hardware. No server.py import
needed - this module only depends on hardware/rtc_service.py, which has
no hardware dependency of its own at import time either.

Module-level cache state (_cache/_cache_ts/_rtc_cache/_rtc_cache_ts) is
reset around every test via an autouse fixture, same pattern
tests/test_cpu_history.py already uses for its own module-level state.
"""

import subprocess
import time

import pytest

import meshsrv.time_service as time_service


@pytest.fixture(autouse=True)
def _reset_time_service_state():
    def _reset():
        time_service._cache = {}
        time_service._cache_ts = 0.0
        time_service._rtc_cache = {}
        time_service._rtc_cache_ts = 0.0

    _reset()
    yield
    _reset()


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

    # _probe() falls back to reading the real /etc/timezone off disk
    # whenever the parsed timedatectl timezone is "UTC" (see its own
    # docstring-less fallback block) - left unmocked, this test's result
    # silently depends on the host machine's actual /etc/timezone content
    # (e.g. "Etc/UTC" rather than "UTC" on some systems), which is exactly
    # what broke it during review. Force that read to fail the same way it
    # does on a system with no /etc/timezone file at all, so the mocked
    # "UTC" from _timedatectl_output() above is what actually survives.
    def _raise_file_not_found(self):
        raise FileNotFoundError()

    monkeypatch.setattr(time_service.Path, "read_text", _raise_file_not_found)

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

    result = time_service._probe()  # must not raise

    assert result["synchronized"] is False
    # RTC side is unaffected by the timedatectl failure - still reports
    # quality "rtc" since NTP unsynced + RTC fully ready.
    assert result["quality"] == "rtc"


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
