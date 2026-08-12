"""Phase 2 hardware test: same scenario as tools/test_epaper_weact.py, but
run entirely through modules/display/drivers/weact_154.py's DisplayDriver
interface - no direct SPI/GPIO/protocol-module access from this script.
Mirrors Stage 1's tools/test_epaper_driver.py.

Run directly on the dev node over SSH:

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper_weact_driver.py

DoD (e-Paper Stage 2 plan, Phase 2): the driver passes the same
init/clear/render/sleep scenario as the Phase 1 standalone test, but
reached only through DisplayDriver's abstract methods.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_epaper_weact_driver")

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TEST_LINES = ["MeshCenter", "WeAct 1.54", "via DisplayDriver", "PASS"]


def build_test_image(caps):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("1", (caps.width, caps.height), 1)
    draw = ImageDraw.Draw(image)

    box = 20
    for row in range(caps.height // 2 // box):
        for col in range(caps.width // box):
            if (row + col) % 2 == 0:
                draw.rectangle([col * box, row * box, (col + 1) * box, (row + 1) * box], fill=0)

    try:
        font = ImageFont.truetype(FONT_PATH, 14)
    except OSError:
        font = ImageFont.load_default()

    y = caps.height // 2 + 4
    for line in TEST_LINES:
        draw.text((4, y), line, fill=0, font=font)
        y += 18

    return image


def main() -> int:
    from modules.display.drivers.weact_154 import Weact154Driver
    from modules.display.gpio_registry import GpioConflictError, GpioRegistry

    registry = GpioRegistry()
    driver = Weact154Driver(gpio_registry=registry)

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
        image = build_test_image(driver.capabilities)
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
