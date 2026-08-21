"""Tests for hardware/i2c_service.py - generic I2C bus detection.

No server.py import needed - this module has no dependency on it.
subprocess.run is mocked throughout; nothing here touches real hardware.
"""

import subprocess
from unittest.mock import MagicMock, patch

from hardware import i2c_service

# Captured verbatim from `sudo i2cdetect -y 1` on the real test node (104) -
# a DS3231 already bound to the rtc-ds1307 kernel driver, hence "UU" at
# 0x68 instead of the hex value "68". Row "00:" also leaves columns 0-7
# blank (not "--") on this i2c-tools build - both of these broke a first
# version of the parser that assumed the printed cell text always equals
# the address ("68" reads as 0x68 directly) and used split()-based column
# indexing, which silently mis-locates columns once any field is blank
# instead of "--". This fixture is what caught it during live verification.
I2CDETECT_REAL_DS3231_UU_OUTPUT = (
    "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f\n"
    "00:                         -- -- -- -- -- -- -- -- \n"
    "10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- \n"
    "20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- \n"
    "30: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- \n"
    "40: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- \n"
    "50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- \n"
    "60: -- -- -- -- -- -- -- -- UU -- -- -- -- -- -- -- \n"
    "70: -- -- -- -- -- -- -- --                         \n"
)

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


def test_scan_bus_treats_uu_cell_as_detected_at_its_real_address():
    # "UU" means the address is claimed by an already-bound kernel driver -
    # still counts as "detected" (the address responded), and the address
    # itself must come from the cell's row+column position, not its text
    # ("UU" is not a parseable hex value) - this is the exact scenario a
    # real DS3231 already bound to rtc-ds1307 produces.
    with patch("subprocess.run", return_value=_completed(stdout=I2CDETECT_REAL_DS3231_UU_OUTPUT)):
        result = i2c_service.scan_bus(1)
    assert result == {"ok": True, "bus": 1, "addresses": ["0x68"]}


def test_scan_bus_leading_blank_columns_do_not_shift_addresses():
    # Row "00:" leaves columns 0-7 blank (not "--") on this i2c-tools
    # build - naive split()-based column counting would misread every
    # subsequent row's column offset once it hit this row. Position-based
    # parsing must be immune to it; assert the *other* rows in the same
    # output still resolve to the correct addresses.
    with patch("subprocess.run", return_value=_completed(stdout=I2CDETECT_REAL_DS3231_UU_OUTPUT)):
        result = i2c_service.scan_bus(1)
    assert result["addresses"] == ["0x68"]


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
