"""WeAct Studio 1.54" 200x200 monochrome e-paper module driver. e-Paper
Stage 2 plan (WeAct 1.54"), Phase 2.

Wraps _weact_ssd1681.py's protocol implementation (original code, not
vendored - see _weact_ssd1681_LICENSE_NOTICE.md) behind the DisplayDriver
interface, same shape as waveshare_213g.py from Stage 1. Unlike that
driver, no monkey-patching of vendor-owned globals is needed here -
_weact_ssd1681.Ssd1681 was written from the start with pins/SPI as
constructor arguments, so they're simply passed straight through.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from modules.display.drivers._weact_ssd1681 import Ssd1681
from modules.display.drivers.base import DisplayCapabilities, DisplayDriver
from modules.display.gpio_registry import GpioRegistry

_CAPABILITIES = DisplayCapabilities(
    width=200,
    height=200,
    colors=("black", "white"),
    # Stage 2 plan section 2: off by default even though SSD1681 usually
    # supports it - a deliberate later decision, not part of this
    # baseline integration.
    supports_fast_refresh=False,
)

# Confirmed-working values for the dev node (see
# _weact_ssd1681_LICENSE_NOTICE.md for how pins/SPI mode were sourced,
# and the Phase 1 bring-up for physical wiring confirmation). No PWR pin
# on this board - unlike Stage 1's Waveshare213gDriver, there's no inert
# field to carry here.
DEFAULT_PINS: dict[str, int] = {"rst": 17, "dc": 25, "cs": 8, "busy": 24}
DEFAULT_SPI: dict[str, int] = {"bus": 0, "device": 0}


class Weact154Driver(DisplayDriver):
    id = "weact_154"
    display_name = 'WeAct Studio 1.54" e-Paper Module (SSD1681)'

    def __init__(
        self,
        pins: dict[str, int] | None = None,
        spi: dict[str, int] | None = None,
        gpio_registry: GpioRegistry | None = None,
    ):
        self._pins = {**DEFAULT_PINS, **(pins or {})}
        self._spi = {**DEFAULT_SPI, **(spi or {})}
        self._gpio_registry = gpio_registry
        self._epd: Ssd1681 | None = None
        self._started = False
        self._last_error: str | None = None

    @property
    def capabilities(self) -> DisplayCapabilities:
        return _CAPABILITIES

    def detect(self) -> dict[str, Any] | None:
        """SPI device presence only - doesn't touch GPIO or open a
        session, matching CameraDriver.detect()'s "cheap probe" contract
        (same convention as Waveshare213gDriver.detect())."""
        spi_path = Path(f"/dev/spidev{self._spi['bus']}.{self._spi['device']}")
        if not spi_path.exists():
            return None
        return {"model": self.display_name, "spi_path": str(spi_path)}

    def start(self, **options: Any) -> bool:
        if self._started:
            return True
        if self._gpio_registry is not None:
            self._gpio_registry.claim(self._pins, owner=self.id)
        try:
            epd = Ssd1681(
                rst_pin=self._pins["rst"],
                dc_pin=self._pins["dc"],
                busy_pin=self._pins["busy"],
                spi_bus=self._spi["bus"],
                spi_device=self._spi["device"],
            )
            epd.init()
            self._epd = epd
            self._started = True
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def stop(self) -> None:
        """Always releases the GPIO registry claim, even if this driver
        never got past a failed start() - see e-Paper Stage 2 plan
        (WeAct 1.54"), Phase 4 notes: start() claims GPIO before
        attempting init(), so a driver that fails partway through start()
        still holds a claim that only stop() can release. An early
        `if not self._started: return` here (an earlier version of this
        method) skipped that release entirely, leaking the claim until
        process restart - this matters in practice for
        DisplayManager.replace_driver()'s reinit-failure rollback path,
        which calls stop() on exactly such a driver."""
        try:
            if self._started and self._epd is not None:
                self._epd.close()
        finally:
            self._epd = None
            self._started = False
            if self._gpio_registry is not None:
                self._gpio_registry.release(self.id)

    def render(self, image: Any, fast: bool = False) -> None:
        self._require_started()
        buf = image.convert("1").tobytes()
        self._epd.display(buf)

    def clear(self) -> None:
        self._require_started()
        self._epd.clear()

    def sleep(self) -> None:
        self._require_started()
        self._epd.sleep()
        self._started = False

    def get_status(self) -> dict[str, Any]:
        return {
            "ok": self._last_error is None,
            "started": self._started,
            "model": self.display_name,
            "error": self._last_error,
        }

    def _require_started(self) -> None:
        if not self._started or self._epd is None:
            raise RuntimeError(f"{self.id}: not started - call start() first")
