"""Read-only BME280 (temperature/humidity/pressure) sensor status.

Second I2C device built on hardware/i2c_service.py after RTC (task 23) -
deliberately used as a check that i2c_service.py's address-detection layer
really is generic and needed zero changes to support a second, unrelated
device type. Unlike RTC, BME280 needs no Device Tree overlay/kernel
driver - it's a plain userspace I2C device, readable the moment the bus
itself is enabled - so there's no three-stage detected/configured/readable
model here, only two: detected (address answers AND its chip ID register
confirms it's actually a BME280, not a look-alike BMP280 or something
else entirely) and readable (register read + Bosch compensation math
succeeded).

Same subprocess-over-i2c-tools pattern as i2c_service.py/rtc_service.py
(no smbus2/pip I2C dependency) - `i2cget`/`i2ctransfer`, both part of the
i2c-tools package already required by task 23. Never raises; every
failure mode (i2c-tools missing, wrong chip ID, a bad register read,
malformed CLI output) becomes a structured {"ok": False, "reason": ...}
at the appropriate stage instead of an exception.
"""

from __future__ import annotations

import subprocess

from hardware import i2c_service

CANDIDATE_ADDRESSES = ("0x76", "0x77")

CHIP_ID_REGISTER = "0xD0"
BME280_CHIP_ID = 0x60
BMP280_CHIP_ID = 0x58

CALIB1_START = "0x88"
CALIB1_LEN = 26  # 0x88-0xA1 inclusive
CALIB2_START = "0xE1"
CALIB2_LEN = 7  # 0xE1-0xE7 inclusive
DATA_START = "0xF7"
DATA_LEN = 8  # 0xF7-0xFE inclusive: press(3) + temp(3) + hum(2)

I2C_TIMEOUT = 10


def _run(args: list) -> tuple[str | None, str | None]:
    """Run an i2c-tools CLI command. Returns (stdout, None) on success or
    (None, reason) on any failure - never raises."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=I2C_TIMEOUT)
    except FileNotFoundError:
        return None, f"{args[0]} not installed (i2c-tools package missing)"
    except subprocess.TimeoutExpired:
        return None, f"{args[0]} timed out after {I2C_TIMEOUT}s"
    except Exception as exc:  # noqa: BLE001 - never raise out of this layer
        return None, f"{args[0]} failed: {exc}"

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip()
        return None, stderr or f"{args[0]} exited with code {result.returncode}"

    return result.stdout, None


def _read_byte(bus: int, address: str, register: str) -> tuple[int | None, str | None]:
    out, err = _run(["i2cget", "-y", str(bus), address, register])
    if err:
        return None, err
    try:
        return int(out.strip(), 16), None
    except ValueError:
        return None, f"unexpected i2cget output: {out!r}"


def _read_block(bus: int, address: str, register: str, length: int) -> tuple[list | None, str | None]:
    """One combined write-register-then-read-N-bytes I2C transaction via
    i2ctransfer, rather than N separate i2cget calls - matches the
    burst-read the Bosch datasheet recommends so the multi-byte raw
    pressure/temperature/humidity registers can't be torn mid-conversion
    by reading them one byte at a time."""
    out, err = _run(["i2ctransfer", "-y", str(bus), f"w1@{address}", register, f"r{length}"])
    if err:
        return None, err
    tokens = out.split()
    try:
        values = [int(token, 16) for token in tokens]
    except ValueError:
        return None, f"unexpected i2ctransfer output: {out!r}"
    if len(values) != length:
        return None, f"expected {length} bytes from i2ctransfer, got {len(values)}"
    return values, None


def _to_signed16(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _to_signed8(value: int) -> int:
    return value - 0x100 if value & 0x80 else value


def _parse_calibration(calib1: list, calib2: list) -> dict:
    """calib1 is the 26 bytes from 0x88-0xA1, calib2 is the 7 bytes from
    0xE1-0xE7 - register layout and signedness straight from the Bosch
    BME280 datasheet's "Compensation formulas" section."""
    def u16(lo_idx):
        return calib1[lo_idx] | (calib1[lo_idx + 1] << 8)

    def s16(lo_idx):
        return _to_signed16(u16(lo_idx))

    dig_T1 = u16(0)
    dig_T2 = s16(2)
    dig_T3 = s16(4)
    dig_P1 = u16(6)
    dig_P2 = s16(8)
    dig_P3 = s16(10)
    dig_P4 = s16(12)
    dig_P5 = s16(14)
    dig_P6 = s16(16)
    dig_P7 = s16(18)
    dig_P8 = s16(20)
    dig_P9 = s16(22)
    dig_H1 = calib1[25]

    dig_H2 = _to_signed16(calib2[0] | (calib2[1] << 8))
    dig_H3 = calib2[2]
    dig_H4 = _to_signed16((calib2[3] << 4) | (calib2[4] & 0x0F))
    dig_H5 = _to_signed16((calib2[5] << 4) | (calib2[4] >> 4))
    dig_H6 = _to_signed8(calib2[6])

    return {
        "T1": dig_T1, "T2": dig_T2, "T3": dig_T3,
        "P1": dig_P1, "P2": dig_P2, "P3": dig_P3, "P4": dig_P4, "P5": dig_P5,
        "P6": dig_P6, "P7": dig_P7, "P8": dig_P8, "P9": dig_P9,
        "H1": dig_H1, "H2": dig_H2, "H3": dig_H3, "H4": dig_H4, "H5": dig_H5, "H6": dig_H6,
    }


def _compensate_temperature(adc_T: int, cal: dict) -> tuple[int, int]:
    """Returns (T_centidegrees, t_fine) - t_fine feeds into the pressure
    and humidity formulas below, T is in units of 0.01 degC."""
    var1 = (((adc_T >> 3) - (cal["T1"] << 1)) * cal["T2"]) >> 11
    var2 = (((((adc_T >> 4) - cal["T1"]) * ((adc_T >> 4) - cal["T1"])) >> 12) * cal["T3"]) >> 14
    t_fine = var1 + var2
    T = (t_fine * 5 + 128) >> 8
    return T, t_fine


def _compensate_pressure(adc_P: int, t_fine: int, cal: dict) -> float:
    """Returns pressure in Pa. 64-bit integer formula from the datasheet
    (bme280_compensate_P_int64)."""
    var1 = t_fine - 128000
    var2 = var1 * var1 * cal["P6"]
    var2 = var2 + ((var1 * cal["P5"]) << 17)
    var2 = var2 + (cal["P4"] << 35)
    var1 = ((var1 * var1 * cal["P3"]) >> 8) + ((var1 * cal["P2"]) << 12)
    var1 = ((1 << 47) + var1) * cal["P1"] >> 33
    if var1 == 0:
        return 0.0  # avoid a division by zero - datasheet's own guard
    p = 1048576 - adc_P
    p = (((p << 31) - var2) * 3125) // var1
    var1 = (cal["P9"] * (p >> 13) * (p >> 13)) >> 25
    var2 = (cal["P8"] * p) >> 19
    p = ((p + var1 + var2) >> 8) + (cal["P7"] << 4)
    return p / 256.0


def _compensate_humidity(adc_H: int, t_fine: int, cal: dict) -> float:
    """Returns relative humidity in %RH. Integer formula from the
    datasheet (bme280_compensate_H_int32)."""
    v_x1 = t_fine - 76800
    v_x1 = (
        ((((adc_H << 14) - (cal["H4"] << 20) - (cal["H5"] * v_x1)) + 16384) >> 15)
        * (
            (
                (
                    ((v_x1 * cal["H6"]) >> 10)
                    * (((v_x1 * cal["H3"]) >> 11) + 32768)
                )
                >> 10
            )
            + 2097152
        )
        * cal["H2"]
        + 8192
    ) >> 14
    v_x1 = v_x1 - (((((v_x1 >> 15) * (v_x1 >> 15)) >> 7) * cal["H1"]) >> 4)
    v_x1 = max(0, min(v_x1, 419430400))
    H = v_x1 >> 12
    return H / 1024.0


def get_status(bus: int = i2c_service.DEFAULT_BUS) -> dict:
    """Full two-stage BME280 status: detected (address + chip ID
    confirmed) and readable (registers read and compensated
    successfully)."""
    scan = i2c_service.scan_bus(bus)
    if not scan.get("ok"):
        return {
            "ok": True,
            "bus": bus,
            "stages": {"detected": {"ok": False, "reason": scan.get("reason")}},
        }

    scanned_addresses = set(scan.get("addresses", []))
    address = next((addr for addr in CANDIDATE_ADDRESSES if addr in scanned_addresses), None)
    if address is None:
        return {
            "ok": True,
            "bus": bus,
            "stages": {
                "detected": {
                    "ok": False,
                    "reason": f"no device answered at {' or '.join(CANDIDATE_ADDRESSES)}",
                }
            },
        }

    chip_id, err = _read_byte(bus, address, CHIP_ID_REGISTER)
    if err:
        return {
            "ok": True,
            "bus": bus,
            "address": address,
            "stages": {"detected": {"ok": False, "reason": err}},
        }

    if chip_id == BMP280_CHIP_ID:
        return {
            "ok": True,
            "bus": bus,
            "address": address,
            "stages": {
                "detected": {
                    "ok": False,
                    "reason": "BMP280 detected at this address, not BME280 (no humidity sensor)",
                }
            },
        }

    if chip_id != BME280_CHIP_ID:
        return {
            "ok": True,
            "bus": bus,
            "address": address,
            "stages": {
                "detected": {
                    "ok": False,
                    "reason": f"unexpected chip ID 0x{chip_id:02x} at {address} - not a BME280",
                }
            },
        }

    result = {
        "ok": True,
        "bus": bus,
        "address": address,
        "stages": {"detected": {"ok": True}},
    }

    calib1, err = _read_block(bus, address, CALIB1_START, CALIB1_LEN)
    if err:
        result["stages"]["readable"] = {"ok": False, "reason": err}
        return result

    calib2, err = _read_block(bus, address, CALIB2_START, CALIB2_LEN)
    if err:
        result["stages"]["readable"] = {"ok": False, "reason": err}
        return result

    raw, err = _read_block(bus, address, DATA_START, DATA_LEN)
    if err:
        result["stages"]["readable"] = {"ok": False, "reason": err}
        return result

    try:
        cal = _parse_calibration(calib1, calib2)

        press_msb, press_lsb, press_xlsb, temp_msb, temp_lsb, temp_xlsb, hum_msb, hum_lsb = raw
        adc_P = (press_msb << 12) | (press_lsb << 4) | (press_xlsb >> 4)
        adc_T = (temp_msb << 12) | (temp_lsb << 4) | (temp_xlsb >> 4)
        adc_H = (hum_msb << 8) | hum_lsb

        temperature_centideg, t_fine = _compensate_temperature(adc_T, cal)
        pressure_pa = _compensate_pressure(adc_P, t_fine, cal)
        humidity_pct = _compensate_humidity(adc_H, t_fine, cal)
    except Exception as exc:  # noqa: BLE001 - never raise out of this layer
        result["stages"]["readable"] = {"ok": False, "reason": f"compensation failed: {exc}"}
        return result

    result["stages"]["readable"] = {"ok": True, "reason": None}
    result["values"] = {
        "temperature_c": round(temperature_centideg / 100.0, 2),
        "humidity_pct": round(humidity_pct, 2),
        "pressure_hpa": round(pressure_pa / 100.0, 2),
    }
    return result
