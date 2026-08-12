"""Original (not vendored) SSD1681 protocol implementation for the WeAct
Studio 1.54" 200x200 monochrome e-paper module. e-Paper Stage 2 plan
(WeAct 1.54"), Phases 1-2. See _weact_ssd1681_LICENSE_NOTICE.md in this
directory for why this is written from scratch against the public SSD1681
protocol rather than vendored from WeAct's C reference (their repo has no
LICENSE), and for exactly which facts (pins, SPI mode, BUSY polarity,
register values) were cross-referenced against that reference.

Phase 1 standalone bring-up (tools/test_epaper_weact.py) passed 3/3 clean
runs against this exact code, after the user physically re-verified the
DIN/CLK/CS/DC wiring - so this module moved here (from its Phase 1
temporary location, tools/_weact_driver/) unmodified, wrapped by
weact_154.py's DisplayDriver implementation in this same directory.
tools/test_epaper_weact.py still imports directly from here as a
minimal-dependency smoke test, same convention as Stage 1's
tools/test_epaper.py.
"""

from __future__ import annotations

import time


class Ssd1681Timeout(Exception):
    pass


class Ssd1681:
    WIDTH = 200
    HEIGHT = 200
    BUFFER_BYTES = ((WIDTH + 7) // 8) * HEIGHT  # 25 * 200 = 5000

    def __init__(
        self,
        rst_pin: int = 17,
        dc_pin: int = 25,
        busy_pin: int = 24,
        spi_bus: int = 0,
        spi_device: int = 0,
        spi_hz: int = 1_000_000,
        busy_timeout_s: float = 40.0,
    ):
        import gpiozero
        import spidev

        self._busy_timeout_s = busy_timeout_s

        self._rst = gpiozero.LED(rst_pin)
        self._dc = gpiozero.LED(dc_pin)
        self._busy = gpiozero.Button(busy_pin, pull_up=False)

        self._spi = spidev.SpiDev()
        self._spi.open(spi_bus, spi_device)
        self._spi.max_speed_hz = spi_hz
        self._spi.mode = 0b11  # SPI mode 3 (CPOL=1, CPHA=1)

    def close(self) -> None:
        self._spi.close()
        self._rst.close()
        self._dc.close()
        self._busy.close()

    def reset(self) -> None:
        self._rst.off()
        time.sleep(0.05)
        self._rst.on()
        time.sleep(0.05)

    def raw_busy_level(self) -> int:
        """Unfiltered BUSY pin read (0 or 1) - used by the standalone
        test's diagnostic step to confirm the assumed HIGH=busy polarity
        against real hardware before wait_busy() trusts it."""
        return int(self._busy.value)

    def _is_busy(self) -> bool:
        return bool(self._busy.value)  # HIGH = busy - see LICENSE_NOTICE.md

    def wait_busy(self) -> None:
        deadline = time.monotonic() + self._busy_timeout_s
        while self._is_busy():
            if time.monotonic() > deadline:
                raise Ssd1681Timeout(f"BUSY did not clear within {self._busy_timeout_s:.0f}s")
            time.sleep(0.005)

    def write_command(self, reg: int) -> None:
        self._dc.off()
        self._spi.writebytes([reg])
        self._dc.on()

    def write_data(self, data) -> None:
        if isinstance(data, int):
            data = [data]
        self._spi.writebytes2(list(data))

    def _set_ram_pos(self, x: int, y: int) -> None:
        col = x // 8
        row = (self.HEIGHT - 1) - y
        self.write_command(0x4E)  # set RAM X address counter
        self.write_data(col)
        self.write_command(0x4F)  # set RAM Y address counter
        self.write_data([row & 0xFF, (row >> 8) & 0x01])

    def init(self) -> None:
        self.reset()
        self.wait_busy()

        self.write_command(0x12)  # SWRESET
        time.sleep(0.1)
        self.wait_busy()

        self.write_command(0x01)  # Driver output control
        self.write_data([0xC7, 0x00, 0x01])

        self.write_command(0x11)  # Data entry mode
        self.write_data(0x01)

        self.write_command(0x44)  # RAM-X address start/end position
        self.write_data([0x00, 0x18])

        self.write_command(0x45)  # RAM-Y address start/end position
        self.write_data([0xC7, 0x00, 0x00, 0x00])

        self.write_command(0x3C)  # Border waveform
        self.write_data(0x05)

        self.write_command(0x18)  # Temperature sensor
        self.write_data(0x80)

        self._set_ram_pos(0, 0)

        self.write_command(0x22)  # Display update control 2
        self.write_data(0xF8)  # power on
        self.write_command(0x20)  # Activate display update sequence
        self.wait_busy()

    def display(self, buf: bytes) -> None:
        """buf must be exactly BUFFER_BYTES (5000) bytes: 1 bit/pixel,
        MSB-first per byte, 1=white/0=black (matches PIL's mode "1"
        .tobytes() packing directly - no bit-twiddling needed by callers).
        Written to both SSD1681 RAM planes (0x26 then 0x24) since a clean
        full refresh needs both, per the vendor reference."""
        if len(buf) != self.BUFFER_BYTES:
            raise ValueError(f"Expected {self.BUFFER_BYTES} bytes, got {len(buf)}")

        self._set_ram_pos(0, 0)
        self.write_command(0x26)
        self.write_data(buf)

        self._set_ram_pos(0, 0)
        self.write_command(0x24)
        self.write_data(buf)

        self.write_command(0x22)  # Display update control 2
        self.write_data(0xF4)  # full refresh
        self.write_command(0x20)  # Activate display update sequence
        self.wait_busy()

    def clear(self, white: bool = True) -> None:
        fill = 0xFF if white else 0x00
        self.display(bytes([fill]) * self.BUFFER_BYTES)

    def sleep(self) -> None:
        self.write_command(0x22)  # Display update control 2
        self.write_data(0x83)  # power off
        self.write_command(0x20)  # Activate display update sequence
        self.wait_busy()

        self.write_command(0x10)  # Deep sleep mode
        self.write_data(0x01)
