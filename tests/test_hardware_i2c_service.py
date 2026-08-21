"""Tests for hardware/i2c_service.py - generic I2C bus detection.

No server.py import needed - this module has no dependency on it.
subprocess.run is mocked throughout; nothing here touches real hardware.
"""

import subprocess
from unittest.mock import MagicMock, patch

from hardware import i2c_service

I2CDETECT_SAMPLE_OUTPUT = """\
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
40: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
60: -- -- -- -- -- -- -- -- 68 -- -- -- -- -- -- --
70: -- -- -- -- -- -- -- --
"""


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_scan_bus_parses_detected_address():
    with patch("subprocess.run", return_value=_completed(stdout=I2CDETECT_SAMPLE_OUTPUT)):
        result = i2c_service.scan_bus(1)
    assert result == {"ok": True, "bus": 1, "addresses": ["0x68"]}


def test_scan_bus_no_devices_detected():
    empty_output = I2CDETECT_SAMPLE_OUTPUT.replace(" 68 ", " -- ")
    with patch("subprocess.run", return_value=_completed(stdout=empty_output)):
        result = i2c_service.scan_bus(1)
    assert result == {"ok": True, "bus": 1, "addresses": []}


def test_scan_bus_multiple_addresses_sorted_and_deduped():
    output = (
        "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\n"
        "20: -- -- -- -- 24 -- -- -- -- -- -- -- -- -- -- --\n"
        "60: -- -- -- -- -- -- -- -- 68 -- -- -- -- -- -- --\n"
    )
    with patch("subprocess.run", return_value=_completed(stdout=output)):
        result = i2c_service.scan_bus(1)
    assert result["ok"] is True
    assert result["addresses"] == ["0x24", "0x68"]


def test_scan_bus_treats_uu_cell_as_detected():
    # "UU" means the address is claimed by an already-bound kernel driver -
    # still counts as "detected" at this layer (the address responded),
    # even though the resulting token isn't a parseable hex address.
    output = "60: -- -- -- -- -- -- -- -- UU -- -- -- -- -- -- --\n"
    with patch("subprocess.run", return_value=_completed(stdout=output)):
        result = i2c_service.scan_bus(1)
    assert result["ok"] is True
    assert len(result["addresses"]) == 1


def test_scan_bus_i2cdetect_not_installed():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        result = i2c_service.scan_bus(1)
    assert result["ok"] is False
    assert "not installed" in result["reason"]


def test_scan_bus_timeout():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="i2cdetect", timeout=10)):
        result = i2c_service.scan_bus(1)
    assert result["ok"] is False
    assert "timed out" in result["reason"]


def test_scan_bus_nonzero_exit_code_bus_missing():
    with patch("subprocess.run", return_value=_completed(stderr="Error: Could not open file", returncode=1)):
        result = i2c_service.scan_bus(9)
    assert result["ok"] is False
    assert result["bus"] == 9
    assert "Could not open file" in result["reason"]


def test_scan_bus_never_raises_on_unexpected_error():
    with patch("subprocess.run", side_effect=OSError("boom")):
        result = i2c_service.scan_bus(1)
    assert result["ok"] is False
    assert "boom" in result["reason"]


def test_scan_bus_passes_bus_number_as_argument_not_shell_string():
    mock_run = MagicMock(return_value=_completed(stdout=I2CDETECT_SAMPLE_OUTPUT))
    with patch("subprocess.run", mock_run):
        i2c_service.scan_bus(3)
    args, kwargs = mock_run.call_args
    assert args[0] == ["i2cdetect", "-y", "3"]
    assert kwargs.get("shell") is not True
