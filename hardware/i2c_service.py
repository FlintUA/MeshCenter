"""Read-only I2C bus detection - a thin, generic wrapper over `i2cdetect`.

Deliberately not RTC-specific: this module only answers "what addresses
respond on this bus", the same question for any I2C peripheral (RTC,
INA219/INA226, BME280, ADS1115, MCP23017, ...). Device-specific
interpretation of an address (e.g. "0x68 is probably a DS3231") lives in
the device's own module (rtc_service.py for RTC), not here.

Never raises - i2cdetect being absent, the bus not existing, or a timeout
are all reported as a structured {"ok": False, "reason": ...} result rather
than an exception, so callers (the API layer) can tell "i2c-tools isn't
installed" apart from "no device answered" instead of both looking like an
empty address list.
"""

from __future__ import annotations

import re
import subprocess

DEFAULT_BUS = 1
I2CDETECT_TIMEOUT = 10

_ADDRESS_CELL_RE = re.compile(r"^(?:[0-9a-fA-F]{2}|UU)$")


def scan_bus(bus: int = DEFAULT_BUS) -> dict:
    """Run `i2cdetect -y <bus>` and return detected addresses.

    Returns {"ok": True, "bus": bus, "addresses": ["0x68", ...]} on success,
    or {"ok": False, "bus": bus, "reason": "..."} if i2cdetect is missing,
    the bus doesn't exist, the call times out, or anything else goes wrong.
    "UU" cells (address claimed by an already-bound kernel driver) count as
    detected - the address responded, that's all this layer answers.
    """
    try:
        result = subprocess.run(
            ["i2cdetect", "-y", str(bus)],
            capture_output=True,
            text=True,
            timeout=I2CDETECT_TIMEOUT,
        )
    except FileNotFoundError:
        return {"ok": False, "bus": bus, "reason": "i2cdetect not installed (i2c-tools package missing)"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "bus": bus, "reason": f"i2cdetect timed out after {I2CDETECT_TIMEOUT}s"}
    except Exception as exc:  # noqa: BLE001 - never raise out of this layer
        return {"ok": False, "bus": bus, "reason": f"i2cdetect failed: {exc}"}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        return {
            "ok": False,
            "bus": bus,
            "reason": stderr or f"i2cdetect exited with code {result.returncode} (bus {bus} may not exist)",
        }

    addresses = _parse_i2cdetect_output(result.stdout or "")
    return {"ok": True, "bus": bus, "addresses": addresses}


def _parse_i2cdetect_output(output: str) -> list:
    """Parse `i2cdetect -y` table output into a sorted list of "0xNN"
    address strings.

    Column position, not the cell's own text, determines the address -
    verified against real i2cdetect output from a device with an
    already-bound driver: a claimed address prints "UU" instead of its
    hex value (e.g. row "60:" column 8 reads "UU", not "68"), and leading
    reserved addresses print as blank fields rather than "--" (row "00:"
    leaves columns 0-7 empty on this system's i2c-tools build). Both cases
    break naive "the cell text already is the address" parsing, so this
    reads each row as fixed-width 3-character fields instead: row "XX:"
    is a 4-character prefix (2 hex digits + colon + space), followed by
    16 columns of "cc " each - address = row_value + column_index,
    regardless of whether the cell holds a hex value, "UU", or is blank.
    """
    addresses = []
    for raw_line in output.splitlines():
        line = raw_line.rstrip("\n")
        if len(line) < 3 or line[2] != ":":
            continue
        row_label = line[:2]
        try:
            row_value = int(row_label, 16)
        except ValueError:
            continue

        remainder = line[4:]  # skip "XX: "
        for column in range(16):
            start = column * 3
            cell = remainder[start:start + 2].strip()
            if _ADDRESS_CELL_RE.match(cell):
                addresses.append(f"0x{row_value + column:02x}")

    return sorted(set(addresses))
