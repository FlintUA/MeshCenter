"""Standalone hardware test for the WeAct Studio 1.54" 200x200 B/W
e-paper module. e-Paper Stage 2 plan (WeAct 1.54"), Phase 1.

Run directly on the dev node over SSH:

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper_weact.py

Uses the original (not vendored - see
modules/display/drivers/_weact_ssd1681_LICENSE_NOTICE.md) SSD1681 protocol
implementation, imported directly from its Phase 2 permanent home
(modules/display/drivers/_weact_ssd1681.py) as a minimal-dependency smoke
test - same convention as Stage 1's tools/test_epaper.py. See
tools/test_epaper_weact_driver.py for the same scenario run through
modules/display/drivers/weact_154.py's DisplayDriver interface instead.

Step 0 (raw BUSY diagnostic, run BEFORE the real init): prints BUSY's
actual level during power-up/reset, so the assumed HIGH=busy polarity
(cross-referenced from WeAct's C reference, then confirmed via a
pull-up/pull-down flip test - see the LICENSE_NOTICE.md) gets checked
against real hardware instead of silently trusted.

Then: init -> clear -> checkerboard + Cyrillic/umlaut text test pattern
(section 50: check crisp black-on-white at small size immediately, not
deferred) -> measure real refresh duration -> sleep -> clean exit.

Phase 1 passed 3/3 clean runs (2026-08-12), after physically re-verifying
DIN/CLK/CS/DC wiring. Stable timings: init ~0.30s, clear ~1.76-1.81s,
display ~1.76s, sleep ~0.14s.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_epaper_weact")

STEP_TIMEOUT_SECONDS = 60
DEFAULT_FIXED_DELAY_SECONDS = 3.0
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TEST_LINES = ["MeshCenter", "WeAct 1.54 TEST", "Вузол Мюнхен äöüß", "PASS"]


class StepTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise StepTimeout(f"Step exceeded {STEP_TIMEOUT_SECONDS}s - check wiring/polarity/pins")


def _timed_step(name, fn, *args, **kwargs):
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(STEP_TIMEOUT_SECONDS)
    t0 = time.monotonic()
    try:
        result = fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    elapsed = time.monotonic() - t0
    log.info("%s took %.2fs", name, elapsed)
    return result, elapsed


def raw_busy_diagnostic(epd):
    """Sample BUSY through power-up/reset, independent of wait_busy()'s
    own polarity assumption, before trusting it for the real init."""
    log.info("--- Raw BUSY diagnostic (before trusting HIGH=busy assumption) ---")
    log.info("BUSY before reset: %d", epd.raw_busy_level())
    epd._rst.off()
    time.sleep(0.05)
    log.info("BUSY during reset (RST low): %d", epd.raw_busy_level())
    epd._rst.on()
    log.info("Sampling BUSY for 2s after reset released...")
    for i in range(10):
        log.info("  t=%.1fs BUSY=%d", i * 0.2, epd.raw_busy_level())
        time.sleep(0.2)
    log.info(
        "If BUSY assumption (HIGH=busy) is correct, expect it to have "
        "settled to a stable 0 (idle) by now, not stuck at 1 or toggling."
    )


def build_test_image(width, height):
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("1", (width, height), 1)  # 1 = white
    draw = ImageDraw.Draw(image)

    # Checkerboard bands (top half) - crisp black/white, no dithering.
    box = 20
    for row in range(height // 2 // box):
        for col in range(width // box):
            if (row + col) % 2 == 0:
                draw.rectangle(
                    [col * box, row * box, (col + 1) * box, (row + 1) * box], fill=0
                )

    try:
        font = ImageFont.truetype(FONT_PATH, 14)
    except OSError:
        log.warning("Could not load %s, falling back to PIL default font", FONT_PATH)
        font = ImageFont.load_default()

    y = height // 2 + 4
    for line in TEST_LINES:
        draw.text((4, y), line, fill=0, font=font)
        y += 18

    return image


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-busy",
        action="store_true",
        help="Bypass wait_busy() entirely; sleep a fixed delay instead (diagnostic only, "
             "for when BUSY doesn't seem to reflect real panel activity).",
    )
    parser.add_argument(
        "--fixed-delay", type=float, default=DEFAULT_FIXED_DELAY_SECONDS,
        help=f"Seconds to sleep per step when --no-busy is set (default: {DEFAULT_FIXED_DELAY_SECONDS}).",
    )
    return parser.parse_args()


def main() -> int:
    from modules.display.drivers._weact_ssd1681 import Ssd1681, Ssd1681Timeout

    args = parse_args()

    try:
        epd = Ssd1681()
    except Exception:
        log.exception("Failed to construct Ssd1681() - GPIO/SPI backend did not initialize")
        return 1

    if args.no_busy:
        delay = args.fixed_delay
        log.warning(
            "--no-busy: wait_busy() disabled, sleeping %.1fs per call instead. "
            "Diagnostic mode only - not a real pass/fail on BUSY wiring.",
            delay,
        )

        def _fixed_delay_wait_busy():
            log.info("[no-busy] sleeping %.1fs instead of polling BUSY", delay)
            time.sleep(delay)

        epd.wait_busy = _fixed_delay_wait_busy

    raw_busy_diagnostic(epd)

    durations = {}
    try:
        _, durations["init"] = _timed_step("init", epd.init)

        log.info("Clearing display...")
        _, durations["clear"] = _timed_step("clear", epd.clear)

        log.info("Building test image (checkerboard + Cyrillic/umlaut text)...")
        image = build_test_image(epd.WIDTH, epd.HEIGHT)
        buf = image.tobytes()

        log.info("Displaying test image...")
        _, durations["display"] = _timed_step("display", epd.display, buf)

        log.info("Sleeping display...")
        _, durations["sleep"] = _timed_step("sleep", epd.sleep)

    except (StepTimeout, Ssd1681Timeout) as exc:
        log.error("TIMEOUT: %s", exc)
        _safe_close(epd)
        return 1
    except Exception:
        log.exception("Unexpected failure during test sequence")
        _safe_close(epd)
        return 1

    _safe_close(epd)
    log.info("--- Summary ---")
    for step, seconds in durations.items():
        log.info("  %-8s %.2fs", step, seconds)
    if args.no_busy:
        log.info("PASS (--no-busy mode) - now check the physical panel: did it actually update?")
    else:
        log.info("PASS - now check the physical panel: crisp checkerboard, readable Cyrillic/umlaut text")
    return 0


def _safe_close(epd):
    try:
        epd.close()
    except Exception:
        log.exception("Cleanup (close) also failed - GPIO/SPI may be left open")


if __name__ == "__main__":
    sys.exit(main())
