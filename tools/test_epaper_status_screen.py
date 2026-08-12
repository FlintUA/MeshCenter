"""Phase 4 test: renders the Status Screen (modules/display/pages/status.py)
with representative data and pushes it to the real panel through
DisplayManager, exercising:

  - color semantics (plan section 15): radio_status="offline" must render
    red, not decorative.
  - Cyrillic + Latin-extended (umlaut) text in the same string, to confirm
    DejaVu Sans is actually readable at the chosen point size on the real
    250x122 panel (plan section 3's font decision) - not just "doesn't
    crash".

Run directly on the dev node over SSH:

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper_status_screen.py

This is a rendering/readability check, not an automated pass/fail - after
it finishes, look at the physical panel and confirm the text is legible
and radio status shows in red.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_epaper_status_screen")


def main() -> int:
    from modules.display.drivers.waveshare_213g import Waveshare213gDriver
    from modules.display.gpio_registry import GpioRegistry
    from modules.display.manager import DisplayManager
    from modules.display.models import DisplayStatus, EventPriority
    from modules.display.pages.status import StatusScreenData, render

    driver = Waveshare213gDriver(gpio_registry=GpioRegistry())
    manager = DisplayManager(driver)
    manager.start()

    data = StatusScreenData(
        meshcenter_status="online",
        radio_status="offline",  # must render red - color semantics check
        node_name="Вузол Мюнхен äöüß",  # Cyrillic + umlaut readability check
        node_count=7,
        last_rx="14:32",
        cpu_percent=23.5,
        ram_percent=61.0,
        last_update=time.strftime("%H:%M"),
    )

    image = render(driver.capabilities, data)
    log.info("Rendered status screen, pushing via DisplayManager (CRITICAL to skip debounce)...")
    manager.mark_dirty(image, priority=EventPriority.CRITICAL)

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if manager.status == DisplayStatus.ONLINE:
            break
        time.sleep(0.5)
    else:
        log.error("Never reached ONLINE within 60s, status=%s", manager.status)
        manager.stop()
        return 1

    log.info("PASS (mechanically) - now check the physical panel for: red 'offline' radio "
              "status, and legible Cyrillic+umlaut node name")
    manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
