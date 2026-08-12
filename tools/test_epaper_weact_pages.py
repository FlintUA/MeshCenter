"""Phase 3 verification: pushes Status Screen (online/warning/offline
radio states) and the Alert Screen to the real WeAct panel via
DisplayDriver directly (DisplayManager isn't wired to a selectable model
yet - that's Phase 4), so the no-color severity degradation
(renderer.draw_state_text / alert.py's inverted-background fallback) can
be visually confirmed on real B/W hardware, not just assumed to work
because it works on Stage 1's color Waveshare panel.

Run directly on the dev node over SSH (stop meshcenter.service first -
see e-Paper Stage 2 plan notes on GPIO conflicts):

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper_weact_pages.py
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_epaper_weact_pages")

PAUSE_BETWEEN_SECONDS = 6.0


def main() -> int:
    from modules.display.drivers.weact_154 import Weact154Driver
    from modules.display.gpio_registry import GpioRegistry
    from modules.display.pages import alert as alert_page
    from modules.display.pages import status as status_page

    driver = Weact154Driver(gpio_registry=GpioRegistry())
    if not driver.start():
        log.error("start() failed: %s", driver.get_status())
        return 1
    log.info("start() ok")

    scenarios = [
        ("Status - radio ONLINE (plain black text)", status_page.render(driver.capabilities, status_page.StatusScreenData(
            meshcenter_status="online", radio_status="online", node_name="Test Node",
            node_count=5, last_rx="12:34", cpu_percent=10, ram_percent=40, last_update="12:35",
        ))),
        ("Status - radio WARNING (inverted block expected)", status_page.render(driver.capabilities, status_page.StatusScreenData(
            meshcenter_status="online", radio_status="warning", node_name="Test Node",
            node_count=5, last_rx="12:34", cpu_percent=10, ram_percent=40, last_update="12:36",
        ))),
        ("Status - radio OFFLINE (inverted block expected)", status_page.render(driver.capabilities, status_page.StatusScreenData(
            meshcenter_status="online", radio_status="offline", node_name="Test Node",
            node_count=5, last_rx="12:34", cpu_percent=10, ram_percent=40, last_update="12:37",
        ))),
        ("Alert Screen (full inverted background expected)", alert_page.render(driver.capabilities, alert_page.AlertScreenData(
            title="RADIO OFFLINE", reason="Connection lost", node_name="Test Node",
            device_path="/dev/ttyACM0", last_seen="12:34",
        ))),
    ]

    try:
        for label, image in scenarios:
            log.info("Rendering: %s", label)
            driver.render(image)
            log.info("  -> check the panel now")
            time.sleep(PAUSE_BETWEEN_SECONDS)
    except Exception:
        log.exception("Failed mid-sequence")
        driver.stop()
        return 1

    driver.sleep()
    log.info("PASS (mechanically) - now review: did WARNING/OFFLINE rows show an inverted "
              "black block with white text, and did the Alert Screen show a fully "
              "inverted (black background, white text) screen?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
