"""Waveshare 2.13" 4-color (G) e-Paper HAT driver.

Wraps the vendored epd2in13g_v2 demo driver (see
vendor/waveshare_epd/LICENSE_NOTICE.md for provenance - the
RaspberryPi.module_init() BUSY-pin "kick" documented there is what makes
this panel respond at all) behind the DisplayDriver interface. Direct
successor to tools/test_epaper.py's Phase 1 standalone test: same vendor
code, same confirmed-working pins, now only reachable through this class
instead of importing waveshare_epd directly.

GPIO pins and SPI bus/device come from configuration (constructor), never
hardcoded, per the e-Paper Stage 1 plan's requirement (section 4). The
vendored epdconfig.py itself hardcodes pins as RaspberryPi class attributes
and auto-instantiates that class at import time - there's no clean
injection point for pins/SPI bus in the vendor code, so
_configure_vendor_pins() below reconfigures the already-constructed
instance in place instead.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from modules.display.drivers.base import DisplayCapabilities, DisplayDriver
from modules.display.gpio_registry import GpioRegistry

_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(_VENDOR_DIR))

_CAPABILITIES = DisplayCapabilities(
    width=122,
    height=250,
    colors=("black", "white", "yellow", "red"),
    supports_fast_refresh=True,
)

# Confirmed-working defaults for the dev node's standard 40-pin HAT
# connection (see vendor/waveshare_epd/LICENSE_NOTICE.md and the e-Paper
# Stage 1 plan thread) - also match the product manual's own Raspberry Pi
# pin-correspondence table.
DEFAULT_PINS: dict[str, int] = {"rst": 17, "dc": 25, "cs": 8, "busy": 24, "pwr": 18}
DEFAULT_SPI: dict[str, int] = {"bus": 0, "device": 0}


class Waveshare213gDriver(DisplayDriver):
    id = "waveshare_213g"
    display_name = 'Waveshare 2.13" e-Paper HAT (G)'

    def __init__(
        self,
        pins: dict[str, int] | None = None,
        spi: dict[str, int] | None = None,
        gpio_registry: GpioRegistry | None = None,
    ):
        self._pins = {**DEFAULT_PINS, **(pins or {})}
        self._spi = {**DEFAULT_SPI, **(spi or {})}
        self._gpio_registry = gpio_registry
        self._epd = None  # vendor EPD() instance, created in start()
        self._epdconfig = None
        self._started = False
        self._last_error: str | None = None

    @property
    def capabilities(self) -> DisplayCapabilities:
        return _CAPABILITIES

    def detect(self) -> dict[str, Any] | None:
        """SPI device presence only - doesn't touch GPIO or open a
        session, matching CameraDriver.detect()'s "cheap probe" contract."""
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
            epdconfig, epd2in13g_v2 = self._configure_vendor_pins()
            epd = epd2in13g_v2.EPD()
            if epd.init() != 0:
                self._last_error = "vendor EPD.init() returned non-zero"
                return False
            self._epdconfig = epdconfig
            self._epd = epd
            self._started = True
            self._last_error = None
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def stop(self) -> None:
        if not self._started:
            return
        try:
            self._epdconfig.module_exit()
        finally:
            self._epd = None
            self._started = False
            if self._gpio_registry is not None:
                self._gpio_registry.release(self.id)

    def render(self, image: Any, fast: bool = False) -> None:
        self._require_started()
        buf = self._epd.getbuffer(image)
        self._epd.display(buf)

    def clear(self) -> None:
        self._require_started()
        self._epd.Clear()

    def sleep(self) -> None:
        self._require_started()
        self._epd.sleep()  # vendor sleep() also calls module_exit() internally
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

    def _configure_vendor_pins(self):
        """Import the vendored epdconfig/epd2in13g_v2 modules and
        reconfigure their auto-constructed RaspberryPi implementation to
        use this driver's pins instead of the vendor's hardcoded class
        attributes. gpiozero pin objects can't have their pin changed
        after construction, so the existing ones are closed and recreated
        at the configured pins."""
        import gpiozero
        from waveshare_epd import epd2in13g_v2, epdconfig

        impl = epdconfig.implementation
        pins = self._pins

        impl.GPIO_RST_PIN.close()
        impl.GPIO_DC_PIN.close()
        impl.GPIO_PWR_PIN.close()
        impl.GPIO_BUSY_PIN.close()

        impl.RST_PIN, impl.DC_PIN = pins["rst"], pins["dc"]
        impl.CS_PIN, impl.BUSY_PIN, impl.PWR_PIN = pins["cs"], pins["busy"], pins["pwr"]

        impl.GPIO_RST_PIN = gpiozero.LED(impl.RST_PIN)
        impl.GPIO_DC_PIN = gpiozero.LED(impl.DC_PIN)
        impl.GPIO_PWR_PIN = gpiozero.LED(impl.PWR_PIN)
        impl.GPIO_BUSY_PIN = gpiozero.Button(impl.BUSY_PIN, pull_up=False)

        impl.module_init = self._make_module_init(impl, self._spi["bus"], self._spi["device"])

        for name in [x for x in dir(impl) if not x.startswith("_")]:
            setattr(epdconfig, name, getattr(impl, name))

        return epdconfig, epd2in13g_v2

    @staticmethod
    def _make_module_init(impl, spi_bus: int, spi_device: int):
        """Re-implements RaspberryPi.module_init() from the vendored
        epdconfig.py, parameterized by SPI bus/device instead of the
        vendor's hardcoded (0, 0) - the vendor code offers no override
        hook for this. Must be kept in sync with that method (including
        the BUSY-pin "kick") if the vendored file is ever updated - see
        vendor/waveshare_epd/LICENSE_NOTICE.md."""
        import gpiozero

        def module_init(cleanup: bool = False) -> int:
            impl.GPIO_PWR_PIN.on()
            impl.GPIO_RST_PIN.on()
            impl.GPIO_DC_PIN.on()

            impl.GPIO_BUSY_PIN.close()
            impl.GPIO_BUSY_PIN = gpiozero.LED(impl.BUSY_PIN)
            impl.GPIO_BUSY_PIN.on()
            impl.delay_ms(20)
            impl.GPIO_BUSY_PIN.close()
            impl.GPIO_BUSY_PIN = gpiozero.Button(impl.BUSY_PIN, pull_up=False)

            if not cleanup:
                impl.SPI.open(spi_bus, spi_device)
                impl.SPI.max_speed_hz = 4000000
                impl.SPI.mode = 0b00
            return 0

        return module_init
