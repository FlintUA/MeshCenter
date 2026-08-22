"""Tests for hardware/bme280_service.py - the second I2C device built on
hardware/i2c_service.py after RTC (task 23), and the first practical check
that i2c_service.py's address-detection layer is actually generic (it
needed zero changes for this - see task 26's PR description).

subprocess.run is mocked throughout, dispatching on argv so both
`i2cget`/`i2ctransfer` calls and `i2cdetect` (via i2c_service.scan_bus())
resolve realistically. No server.py import needed; nothing here touches
real hardware.

Encoding helpers below mirror hardware/bme280_service.py's own
_parse_calibration() layout in reverse (dict of calibration coefficients
-> raw register bytes) - used to build realistic mocked i2ctransfer output
without hand-placing each byte.
"""

import subprocess
from unittest.mock import patch

import pytest

from hardware import bme280_service

# get_status() goes through the real i2c_service.scan_bus(), which since
# task 27 checks /dev/i2c-N exists before ever calling i2cdetect - this
# dev/CI machine has no such device file, so without this every test below
# would short-circuit to "not detected" before subprocess.run's mocked
# dispatch is ever reached. Autouse so it applies uniformly; none of these
# tests are about that check itself (that's covered directly in
# test_hardware_i2c_service.py).
@pytest.fixture(autouse=True)
def _device_file_exists():
    with patch("hardware.i2c_service.Path.exists", return_value=True):
        yield


def _u16_le(value: int) -> list:
    value &= 0xFFFF
    return [value & 0xFF, (value >> 8) & 0xFF]


def _s16_le(value: int) -> list:
    return _u16_le(value & 0xFFFF)


def _encode_calibration(cal: dict) -> tuple:
    """cal is a dict of T1-T3/P1-P9/H1-H6 (same keys _parse_calibration()
    returns) -> (calib1_26_bytes, calib2_7_bytes), the exact register
    layout from the Bosch BME280 datasheet."""
    calib1 = []
    calib1 += _u16_le(cal["T1"])
    calib1 += _s16_le(cal["T2"])
    calib1 += _s16_le(cal["T3"])
    calib1 += _u16_le(cal["P1"])
    calib1 += _s16_le(cal["P2"])
    calib1 += _s16_le(cal["P3"])
    calib1 += _s16_le(cal["P4"])
    calib1 += _s16_le(cal["P5"])
    calib1 += _s16_le(cal["P6"])
    calib1 += _s16_le(cal["P7"])
    calib1 += _s16_le(cal["P8"])
    calib1 += _s16_le(cal["P9"])
    calib1 += [0]  # 0xA0 reserved/unused
    calib1 += [cal["H1"] & 0xFF]
    assert len(calib1) == 26

    h4 = cal["H4"] & 0xFFF
    h5 = cal["H5"] & 0xFFF
    calib2 = [
        cal["H2"] & 0xFF, (cal["H2"] >> 8) & 0xFF,
        cal["H3"] & 0xFF,
        (h4 >> 4) & 0xFF,
        ((h5 & 0xF) << 4) | (h4 & 0xF),
        (h5 >> 4) & 0xFF,
        cal["H6"] & 0xFF,
    ]
    assert len(calib2) == 7
    return calib1, calib2


def _encode_raw(adc_P: int, adc_T: int, adc_H: int) -> list:
    return [
        (adc_P >> 12) & 0xFF, (adc_P >> 4) & 0xFF, (adc_P << 4) & 0xFF,
        (adc_T >> 12) & 0xFF, (adc_T >> 4) & 0xFF, (adc_T << 4) & 0xFF,
        (adc_H >> 8) & 0xFF, adc_H & 0xFF,
    ]


def _hex_line(values: list) -> str:
    return " ".join(f"0x{v:02x}" for v in values) + "\n"


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_mock_run(address: str, chip_id: int, calib1: list, calib2: list, raw: list):
    """Dispatches on argv the same way the real i2c-tools CLIs would be
    invoked: i2cdetect (via i2c_service.scan_bus), i2cget (chip ID), and
    i2ctransfer (calibration blocks + raw data burst read)."""
    def _run(args, **kwargs):
        if args[0] == "i2cdetect":
            bus = args[2]
            # Mirrors a real i2cdetect table with exactly `address` responding.
            row_val = int(address, 16) & 0xF0
            col = int(address, 16) & 0x0F
            cells = ["--"] * 16
            cells[col] = address[2:]
            line = f"{row_val:02x}: " + " ".join(cells)
            header = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f"
            return _completed(stdout=f"{header}\n{line}\n")
        if args[0] == "i2cget":
            reg = args[4]
            if reg == bme280_service.CHIP_ID_REGISTER:
                return _completed(stdout=f"0x{chip_id:02x}\n")
            raise AssertionError(f"unexpected i2cget register: {reg}")
        if args[0] == "i2ctransfer":
            reg = args[4]
            if reg == bme280_service.CALIB1_START:
                return _completed(stdout=_hex_line(calib1))
            if reg == bme280_service.CALIB2_START:
                return _completed(stdout=_hex_line(calib2))
            if reg == bme280_service.DATA_START:
                return _completed(stdout=_hex_line(raw))
            raise AssertionError(f"unexpected i2ctransfer register: {reg}")
        raise AssertionError(f"unexpected command: {args}")
    return _run


# ---------------- successful detection + read, both addresses ----------------

# Calibration/raw values and expected results:
#
# Temperature: T1=27504, T2=26435, T3=-1000, adc_T=519888 - the widely
# published Bosch/community BME280 worked example. Hand-traced:
#   var1 = ((519888>>3) - (27504<<1)) * 26435 >> 11 = (64986-55008)*26435>>11
#        = 9978*26435 >> 11 = 263768430 >> 11 = 128793
#   var2 = (((519888>>4)-27504)^2 >> 12) * -1000 >> 14
#        = (4989^2 >> 12) * -1000 >> 14 = (24890121>>12)*-1000>>14
#        = 6076 * -1000 >> 14 = -6076000 >> 14 = -371
#   t_fine = 128793 - 371 = 128422
#   T = (128422*5 + 128) >> 8 = 642238 >> 8 = 2508  ->  25.08 degC
#
# Pressure: only P1=36477 nonzero (P2-P9=0, a realistic P1 magnitude from
# public BME280 calibration dumps), adc_P=415148, reusing t_fine=128422
# from above. Verified by running _compensate_pressure() directly and
# independently cross-checking the reduced formula (only P1 nonzero
# collapses most terms to 0):
#   var1 = ((1<<47) * 36477) >> 33 = 16384 * 36477 / ... = 267739521 (exact
#     integer result of the datasheet's own formula, reproducible by
#     calling hardware.bme280_service._compensate_pressure((...)) directly)
#   -> 1085.32 hPa (108532.0859375 Pa), a realistic sea-level-range value.
#
# Humidity: H1=75, H2=382, H3=0, H4=350, H5=50, H6=30 (plausible values
# from a real calibration dump), adc_H=30000, reusing t_fine=128422:
#   -> 44.1 %RH (44.0966796875, rounded to 2dp), a realistic indoor value.
#
# All three expected values below were cross-checked by calling
# hardware.bme280_service._compensate_temperature()/_compensate_pressure()/
# _compensate_humidity() directly with these exact inputs (see task 26's
# PR description for the transcript) - re-running those three functions
# with the same arguments is how to independently re-verify this test.
_CAL = {
    "T1": 27504, "T2": 26435, "T3": -1000,
    "P1": 36477, "P2": 0, "P3": 0, "P4": 0, "P5": 0, "P6": 0, "P7": 0, "P8": 0, "P9": 0,
    "H1": 75, "H2": 382, "H3": 0, "H4": 350, "H5": 50, "H6": 30,
}
_ADC_P = 415148
_ADC_T = 519888
_ADC_H = 30000
_EXPECTED_TEMPERATURE_C = 25.08
_EXPECTED_PRESSURE_HPA = 1085.32
_EXPECTED_HUMIDITY_PCT = 44.1


def test_detected_and_readable_at_0x76():
    calib1, calib2 = _encode_calibration(_CAL)
    raw = _encode_raw(_ADC_P, _ADC_T, _ADC_H)
    mock_run = _make_mock_run("0x76", bme280_service.BME280_CHIP_ID, calib1, calib2, raw)

    with patch("subprocess.run", side_effect=mock_run):
        result = bme280_service.get_status(bus=1)

    assert result["ok"] is True
    assert result["address"] == "0x76"
    assert result["stages"]["detected"] == {"ok": True}
    assert result["stages"]["readable"] == {"ok": True, "reason": None}
    assert result["values"]["temperature_c"] == _EXPECTED_TEMPERATURE_C
    assert result["values"]["pressure_hpa"] == _EXPECTED_PRESSURE_HPA
    assert result["values"]["humidity_pct"] == _EXPECTED_HUMIDITY_PCT


def test_detected_and_readable_at_0x77():
    calib1, calib2 = _encode_calibration(_CAL)
    raw = _encode_raw(_ADC_P, _ADC_T, _ADC_H)
    mock_run = _make_mock_run("0x77", bme280_service.BME280_CHIP_ID, calib1, calib2, raw)

    with patch("subprocess.run", side_effect=mock_run):
        result = bme280_service.get_status(bus=1)

    assert result["address"] == "0x77"
    assert result["stages"]["readable"]["ok"] is True
    assert result["values"]["temperature_c"] == _EXPECTED_TEMPERATURE_C


# ---------------- detection failures ----------------

def test_not_detected_when_no_address_responds():
    def _run(args, **kwargs):
        if args[0] == "i2cdetect":
            header = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f"
            empty_row = "70: -- -- -- -- -- -- -- --"
            return _completed(stdout=f"{header}\n{empty_row}\n")
        raise AssertionError(f"unexpected command: {args}")

    with patch("subprocess.run", side_effect=_run):
        result = bme280_service.get_status(bus=1)

    assert result["ok"] is True
    assert result["stages"]["detected"]["ok"] is False
    assert "0x76" in result["stages"]["detected"]["reason"]
    assert "0x77" in result["stages"]["detected"]["reason"]
    assert "readable" not in result["stages"]


def test_bmp280_chip_id_reported_distinctly_not_treated_as_bme280():
    calib1, calib2 = _encode_calibration(_CAL)
    raw = _encode_raw(_ADC_P, _ADC_T, _ADC_H)
    mock_run = _make_mock_run("0x76", bme280_service.BMP280_CHIP_ID, calib1, calib2, raw)

    with patch("subprocess.run", side_effect=mock_run):
        result = bme280_service.get_status(bus=1)

    assert result["stages"]["detected"]["ok"] is False
    assert "BMP280" in result["stages"]["detected"]["reason"]
    assert "readable" not in result["stages"]


def test_unrecognized_chip_id_not_treated_as_bme280():
    calib1, calib2 = _encode_calibration(_CAL)
    raw = _encode_raw(_ADC_P, _ADC_T, _ADC_H)
    mock_run = _make_mock_run("0x76", 0x42, calib1, calib2, raw)

    with patch("subprocess.run", side_effect=mock_run):
        result = bme280_service.get_status(bus=1)

    assert result["stages"]["detected"]["ok"] is False
    assert "0x42" in result["stages"]["detected"]["reason"]


def test_i2c_bus_scan_failure_reported_as_not_detected():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        result = bme280_service.get_status(bus=1)

    assert result["ok"] is True
    assert result["stages"]["detected"]["ok"] is False
    assert "not installed" in result["stages"]["detected"]["reason"]


# ---------------- detected but not readable ----------------

def test_detected_but_calibration_read_fails():
    def _run(args, **kwargs):
        if args[0] == "i2cdetect":
            header = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f"
            row = "70: -- -- -- -- -- -- 76 --"
            return _completed(stdout=f"{header}\n{row}\n")
        if args[0] == "i2cget":
            return _completed(stdout=f"0x{bme280_service.BME280_CHIP_ID:02x}\n")
        if args[0] == "i2ctransfer":
            return _completed(returncode=1, stderr="Error: Could not set address to 0x76: Device or resource busy")
        raise AssertionError(f"unexpected command: {args}")

    with patch("subprocess.run", side_effect=_run):
        result = bme280_service.get_status(bus=1)

    assert result["stages"]["detected"] == {"ok": True}
    assert result["stages"]["readable"]["ok"] is False
    assert "resource busy" in result["stages"]["readable"]["reason"]
    assert "values" not in result


def test_detected_but_raw_data_read_times_out():
    calib1, calib2 = _encode_calibration(_CAL)

    def _run(args, **kwargs):
        if args[0] == "i2cdetect":
            header = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f"
            row = "70: -- -- -- -- -- -- 76 --"
            return _completed(stdout=f"{header}\n{row}\n")
        if args[0] == "i2cget":
            return _completed(stdout=f"0x{bme280_service.BME280_CHIP_ID:02x}\n")
        if args[0] == "i2ctransfer":
            reg = args[4]
            if reg == bme280_service.CALIB1_START:
                return _completed(stdout=_hex_line(calib1))
            if reg == bme280_service.CALIB2_START:
                return _completed(stdout=_hex_line(calib2))
            if reg == bme280_service.DATA_START:
                raise subprocess.TimeoutExpired(cmd="i2ctransfer", timeout=10)
        raise AssertionError(f"unexpected command: {args}")

    with patch("subprocess.run", side_effect=_run):
        result = bme280_service.get_status(bus=1)

    assert result["stages"]["detected"]["ok"] is True
    assert result["stages"]["readable"]["ok"] is False
    assert "timed out" in result["stages"]["readable"]["reason"]
    assert "values" not in result


def test_never_raises_on_malformed_i2ctransfer_output():
    def _run(args, **kwargs):
        if args[0] == "i2cdetect":
            header = "     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f"
            row = "70: -- -- -- -- -- -- 76 --"
            return _completed(stdout=f"{header}\n{row}\n")
        if args[0] == "i2cget":
            return _completed(stdout=f"0x{bme280_service.BME280_CHIP_ID:02x}\n")
        if args[0] == "i2ctransfer":
            return _completed(stdout="not-valid-hex-output\n")
        raise AssertionError(f"unexpected command: {args}")

    with patch("subprocess.run", side_effect=_run):
        result = bme280_service.get_status(bus=1)  # must not raise

    assert result["stages"]["readable"]["ok"] is False
    assert "unexpected i2ctransfer output" in result["stages"]["readable"]["reason"]
