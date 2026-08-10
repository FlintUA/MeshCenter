"""Phase 2 hardware test: same scenario as tools/test_epaper.py, but run
entirely through modules/display/drivers/waveshare_213g.py's DisplayDriver
interface - no direct SPI/GPIO/vendor-module access from this script.

Run directly on the dev node over SSH:

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper_driver.py

DoD (e-Paper Stage 1 plan, Phase 2): the driver passes the same
init/clear/render/sleep scenario as the Phase 1 standalone test, but
reached only through DisplayDriver's abstract methods.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Repo root, so `modules.display...` resolves regardless of cwd - matches
# how server.py itself is normally run from the repo root, but this script
# is invoked directly (python3 tools/test_epaper_driver.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_epaper_driver")


def main() -> int:
    from modules.display.drivers.waveshare_213g import Waveshare213gDriver
    from modules.display.gpio_registry import GpioConflictError, GpioRegistry
    from modules.display.pages import test_pattern

    registry = GpioRegistry()
    driver = Waveshare213gDriver(gpio_registry=registry)

    log.info("detect() -> %s", driver.detect())

    try:
        started = driver.start()
    except GpioConflictError as exc:
        log.error("GPIO conflict on start(): %s", exc)
        return 1

    if not started:
        log.error("start() failed: %s", driver.get_status())
        return 1
    log.info("start() ok, status=%s", driver.get_status())

    try:
        log.info("clear()...")
        driver.clear()

        log.info("render()...")
        image = test_pattern.render(driver.capabilities)
        driver.render(image)

        log.info("sleep()...")
        driver.sleep()
    except Exception:
        log.exception("Test sequence failed")
        driver.stop()
        return 1

    driver.stop()
    log.info("PASS - init/clear/render/sleep all completed via DisplayDriver, no direct SPI/GPIO access")
    return 0


if __name__ == "__main__":
    sys.exit(main())
