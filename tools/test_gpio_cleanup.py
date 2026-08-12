"""Hardware test for the Waveshare213gDriver.stop() GPIO-cleanup fix
(module_exit(cleanup=True) - see waveshare_213g.py's stop()).

GpioRegistry alone can't prove GPIO is actually free at the OS/pin-factory
level - it's a Python-process dict that will say "released" the moment
release() is called, regardless of whether the underlying gpiozero pin
objects were ever .close()'d. This test bypasses GpioRegistry entirely and
tries to re-claim the exact same physical pins directly through gpiozero,
the same library the vendor driver uses - if the pin is genuinely free,
construction succeeds; if the vendor driver only .off()'d it without
.close()'ing it, gpiozero/lgpio raises (pin still owned by the old,
supposedly-stopped driver's pin objects, since Waveshare213gDriver.stop()
never dropped its references to self._epdconfig's still-open pin objects).

Run directly on the dev node over SSH (stop meshcenter.service first, or
this will conflict with the live driver instead of testing the intended
scenario):

    (venv) flint@meshcenter-test:~/meshcenter$ sudo systemctl stop meshcenter.service
    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_gpio_cleanup.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_gpio_cleanup")


def main() -> int:
    from modules.display.drivers.waveshare_213g import DEFAULT_PINS, Waveshare213gDriver
    from modules.display.gpio_registry import GpioRegistry

    driver = Waveshare213gDriver(gpio_registry=GpioRegistry())
    if not driver.start():
        log.error("start() failed: %s", driver.get_status())
        return 1
    log.info("start() ok - pins claimed: %s", DEFAULT_PINS)

    driver.stop()
    log.info("stop() done")

    # Bypass GpioRegistry completely - reclaim the exact same physical pins
    # straight through gpiozero, the way any unrelated process (or a
    # freshly re-init'd driver instance) would.
    import gpiozero

    reclaimed = []
    try:
        rst = gpiozero.LED(DEFAULT_PINS["rst"])
        reclaimed.append(rst)
        dc = gpiozero.LED(DEFAULT_PINS["dc"])
        reclaimed.append(dc)
        pwr = gpiozero.LED(DEFAULT_PINS["pwr"])
        reclaimed.append(pwr)
        busy = gpiozero.Button(DEFAULT_PINS["busy"], pull_up=False)
        reclaimed.append(busy)
    except Exception as exc:
        log.error(
            "FAIL: could not reclaim pins after stop() - GPIO still held "
            "at the OS level: %r",
            exc,
        )
        for obj in reclaimed:
            obj.close()
        return 1

    for obj in reclaimed:
        obj.close()

    log.info("PASS: all 4 pins (rst/dc/pwr/busy) successfully reclaimed "
              "directly via gpiozero after stop() - GPIO genuinely released")
    return 0


if __name__ == "__main__":
    sys.exit(main())
