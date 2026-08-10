"""Standalone hardware test for the Waveshare 2.13" 4-color (G) e-Paper HAT.

Phase 1 of the e-Paper Stage 1 plan (see the plan doc for full context).
Not wired into the Flask app - run directly on the dev node over SSH:

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_epaper.py

Uses the temporary vendored V2 driver (epd2in13g_v2, the one the manual's
Python demo instructions actually run) in tools/_vendor/waveshare_epd/ (see
LICENSE_NOTICE.md there - the epdconfig.py fix documented there, sourced
from Waveshare's official product-page ZIP rather than GitHub's stale
`master` branch, is what makes BUSY work at all). That location and this
script are both temporary - Phase 2 replaces them with
modules/display/drivers/waveshare_213g.py behind the DisplayDriver
interface, with configurable pins instead of the hardcoded class attributes
this test relies on.

Scenario (plan section 48): init -> clear -> all 4 colors -> text -> BUSY
wait via polling with a timeout watchdog -> measure/print durations -> sleep
-> clean exit.

Display is connected via the standard 40-pin HAT connector, so pins are the
unmodified vendor defaults (RST=17, DC=25, CS=8, BUSY=24, PWR=18), confirmed
against the manual's own Raspberry Pi pin table - see
_vendor/waveshare_epd/epdconfig.py / LICENSE_NOTICE.md.

--no-busy: diagnostic-only mode for when BUSY (GPIO24) reads as electrically
floating (confirmed via raw gpiozero pull-up/pull-down probing - see the
e-Paper Stage 1 plan thread). Instead of polling BUSY, sleeps a fixed
--fixed-delay seconds after each command that would normally wait on it.
This is NOT the final design (the plan explicitly wants BUSY polling with a
timeout) - it exists only to answer "does the panel respond to SPI commands
at all", independent of whatever is wrong with the BUSY line. Once BUSY is
fixed, this flag should not be needed.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "_vendor"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_epaper")

REFRESH_TIMEOUT_SECONDS = 90
DEFAULT_FIXED_DELAY_SECONDS = 35.0
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
TEST_LINES = ["MeshCenter", "EPAPER TEST", "Rev2.1", "250x122", "PASS"]


class RefreshTimeout(Exception):
    pass


def _alarm_handler(signum, frame):
    raise RefreshTimeout(f"No BUSY release within {REFRESH_TIMEOUT_SECONDS}s - check wiring/pins")


def _timed_step(name, fn, *args, **kwargs):
    """Run fn under a SIGALRM watchdog, print how long it actually took."""
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(REFRESH_TIMEOUT_SECONDS)
    t0 = time.monotonic()
    try:
        result = fn(*args, **kwargs)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    elapsed = time.monotonic() - t0
    log.info("%s took %.2fs", name, elapsed)
    return result, elapsed


def build_test_image(epd):
    from PIL import Image, ImageDraw, ImageFont

    # Landscape orientation: getbuffer() rotates automatically when the
    # image is (height, width) instead of (width, height).
    w, h = epd.height, epd.width  # 250 x 122
    image = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(image)

    band_w = w // 4
    colors = ["black", "white", "yellow", "red"]
    for i, color in enumerate(colors):
        draw.rectangle([i * band_w, 0, (i + 1) * band_w, h // 2], fill=color)

    try:
        font = ImageFont.truetype(FONT_PATH, 16)
    except OSError:
        log.warning("Could not load %s, falling back to PIL default font", FONT_PATH)
        font = ImageFont.load_default()

    text = " / ".join(TEST_LINES)
    draw.text((4, h // 2 + 4), text, fill="black", font=font)

    return image


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-busy",
        action="store_true",
        help="Bypass BUSY polling entirely; sleep a fixed delay instead (diagnostic only).",
    )
    parser.add_argument(
        "--fixed-delay",
        type=float,
        default=DEFAULT_FIXED_DELAY_SECONDS,
        help=f"Seconds to sleep per step when --no-busy is set (default: {DEFAULT_FIXED_DELAY_SECONDS}).",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    try:
        from waveshare_epd import epd2in13g_v2 as epd2in13g
    except Exception:
        log.exception(
            "Failed to import the vendored epd2in13g_v2 driver - check that "
            "tools/_vendor/waveshare_epd/ exists and that spidev/gpiozero "
            "are importable in this venv (pip list | grep -iE 'spidev|gpiozero')"
        )
        return 1

    try:
        epd = epd2in13g.EPD()
    except Exception:
        log.exception("Failed to construct EPD() - GPIO/SPI backend did not initialize")
        return 1

    if args.no_busy:
        delay = args.fixed_delay
        log.warning(
            "--no-busy: BUSY polling disabled, sleeping %.1fs per step instead. "
            "Diagnostic mode only - not a real pass/fail on BUSY wiring.",
            delay,
        )

        def _fixed_delay_read_busy():
            log.info("[no-busy] sleeping %.1fs instead of polling BUSY", delay)
            time.sleep(delay)

        epd.ReadBusy = _fixed_delay_read_busy

    durations = {}
    try:
        _, durations["init"] = _timed_step("init", epd.init)

        log.info("Clearing display...")
        _, durations["clear"] = _timed_step("clear", epd.Clear)

        log.info("Building test image (4 colors + text)...")
        image = build_test_image(epd)
        buf = epd.getbuffer(image)

        log.info("Displaying test image...")
        _, durations["display"] = _timed_step("display", epd.display, buf)

        log.info("Sleeping display...")
        _, durations["sleep"] = _timed_step("sleep", epd.sleep)

    except RefreshTimeout as exc:
        log.error("TIMEOUT: %s", exc)
        _safe_module_exit(epd2in13g)
        return 1
    except Exception:
        log.exception("Unexpected failure during test sequence")
        _safe_module_exit(epd2in13g)
        return 1

    log.info("--- Summary ---")
    for step, seconds in durations.items():
        log.info("  %-8s %.2fs", step, seconds)
    if args.no_busy:
        log.info("PASS (--no-busy mode - panel responded to SPI commands, BUSY wiring still unverified)")
    else:
        log.info("PASS - all steps completed cleanly, including real BUSY polling")
    return 0


def _safe_module_exit(epd2in13g_module):
    try:
        epd2in13g_module.epdconfig.module_exit()
    except Exception:
        log.exception("Cleanup (module_exit) also failed - GPIO/SPI may be left open")


if __name__ == "__main__":
    sys.exit(main())
