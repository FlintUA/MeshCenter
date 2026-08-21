"""Tests for hardware/rtc_service.py - the three independent RTC status
stages (detected / configured / readable), never collapsed into one flag.

subprocess and the filesystem are mocked throughout; nothing here touches
real hardware. No server.py import needed.
"""

import subprocess
from unittest.mock import patch

from hardware import rtc_service


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _patched(detected_addresses, rtc_device, hwclock_result):
    """Patch all three stage dependencies at once for a given scenario."""
    scan_patch = patch(
        "hardware.i2c_service.scan_bus",
        return_value={"ok": True, "bus": 1, "addresses": detected_addresses},
    )
    device_patch = patch("hardware.rtc_service._find_rtc_device", return_value=rtc_device)
    if isinstance(hwclock_result, Exception):
        hwclock_patch = patch("subprocess.run", side_effect=hwclock_result)
    else:
        hwclock_patch = patch("subprocess.run", return_value=hwclock_result)
    return scan_patch, device_patch, hwclock_patch


def test_all_three_stages_ok():
    scan_p, device_p, hwclock_p = _patched(
        detected_addresses=["0x68"],
        rtc_device="/dev/rtc0",
        hwclock_result=_completed(stdout="2026-08-21 23:59:59.123456+00:00"),
    )
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    assert status["ok"] is True
    assert status["stages"]["detected"] == {"ok": True, "reason": None}
    assert status["stages"]["configured"] == {"ok": True, "reason": None}
    assert status["stages"]["readable"]["ok"] is True
    assert status["stages"]["readable"]["raw_output"] == "2026-08-21 23:59:59.123456+00:00"
    assert status["linux_device"] == "/dev/rtc0"
    assert status["address"] == "0x68"


def test_detected_but_not_configured():
    # Address answers on the bus but the overlay hasn't taken effect yet
    # (e.g. added to config.txt, reboot still pending).
    scan_p, device_p, hwclock_p = _patched(
        detected_addresses=["0x68"],
        rtc_device=None,
        hwclock_result=_completed(returncode=1, stderr="hwclock: ..."),
    )
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    assert status["stages"]["detected"]["ok"] is True
    assert status["stages"]["configured"]["ok"] is False
    # readable never even gets to run hwclock once configured is False.
    assert status["stages"]["readable"]["ok"] is False
    assert "not configured" in status["stages"]["readable"]["reason"]


def test_not_detected_at_all():
    scan_p, device_p, hwclock_p = _patched(
        detected_addresses=[],
        rtc_device=None,
        hwclock_result=_completed(returncode=1),
    )
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    assert status["stages"]["detected"]["ok"] is False
    assert status["stages"]["configured"]["ok"] is False
    assert status["stages"]["readable"]["ok"] is False


def test_configured_but_not_readable():
    # /dev/rtc0 exists (kernel bound something) but hwclock -r fails - e.g.
    # a different RTC model occupies rtc0, or a permissions problem.
    scan_p, device_p, hwclock_p = _patched(
        detected_addresses=["0x68"],
        rtc_device="/dev/rtc0",
        hwclock_result=_completed(returncode=1, stderr="hwclock: select() to /dev/rtc0 failed"),
    )
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    assert status["stages"]["configured"]["ok"] is True
    assert status["stages"]["readable"]["ok"] is False
    assert "select()" in status["stages"]["readable"]["reason"]


def test_i2c_scan_failure_reported_as_detected_reason_not_crash():
    scan_p = patch(
        "hardware.i2c_service.scan_bus",
        return_value={"ok": False, "bus": 1, "reason": "i2cdetect not installed (i2c-tools package missing)"},
    )
    device_p = patch("hardware.rtc_service._find_rtc_device", return_value=None)
    hwclock_p = patch("subprocess.run", return_value=_completed(returncode=1))
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    assert status["stages"]["detected"]["ok"] is False
    assert "not installed" in status["stages"]["detected"]["reason"]


def test_hwclock_garbled_output_does_not_crash_or_get_parsed():
    scan_p, device_p, hwclock_p = _patched(
        detected_addresses=["0x68"],
        rtc_device="/dev/rtc0",
        hwclock_result=_completed(stdout="!!! not a valid timestamp !!!"),
    )
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    # readable=True because hwclock exited 0 - the raw (garbled) text is
    # passed through untouched, never parsed into a structured timestamp.
    assert status["stages"]["readable"]["ok"] is True
    assert status["stages"]["readable"]["raw_output"] == "!!! not a valid timestamp !!!"


def test_hwclock_not_installed():
    scan_p, device_p, hwclock_p = _patched(
        detected_addresses=["0x68"],
        rtc_device="/dev/rtc0",
        hwclock_result=FileNotFoundError(),
    )
    with scan_p, device_p, hwclock_p:
        status = rtc_service.get_status(model="ds3231")

    assert status["stages"]["readable"]["ok"] is False
    assert "not installed" in status["stages"]["readable"]["reason"]


def test_unsupported_model_returns_error_not_crash():
    status = rtc_service.get_status(model="totally-unknown-chip")
    assert status["ok"] is False
    assert "unsupported" in status["reason"]
