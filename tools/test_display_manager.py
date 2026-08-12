"""Phase 3 test: drives DisplayManager programmatically and checks its DoD
(e-Paper Stage 1 plan, Phase 3):

  1. mark_dirty() returns immediately (doesn't block the caller while the
     actual refresh happens on the background worker thread).
  2. Sending the same frame twice doesn't trigger a second physical
     refresh (framebuffer hash dedup).
  3. A refresh that never completes (simulated via a fake stuck driver, so
     this doesn't require waiting out a real 75s hardware timeout) ends in
     Error status without looping forever.

Run directly on the dev node over SSH:

    (venv) flint@meshcenter-test:~/meshcenter$ python3 tools/test_display_manager.py

Part 1 uses the real hardware driver (visible refresh on the physical
panel); Part 2 uses a fake in-memory driver purely to exercise the
timeout/retry/cooldown path quickly and without hardware risk.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("test_display_manager")


def build_test_image(caps, label: str):
    from PIL import Image, ImageDraw, ImageFont

    w, h = caps.height, caps.width
    image = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(image)
    band_w = w // len(caps.colors)
    for i, color in enumerate(caps.colors):
        draw.rectangle([i * band_w, 0, (i + 1) * band_w, h // 2], fill=color)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    draw.text((4, h // 2 + 4), label, fill="black", font=font)
    return image


def part1_real_hardware() -> bool:
    from modules.display.drivers.waveshare_213g import Waveshare213gDriver
    from modules.display.gpio_registry import GpioRegistry
    from modules.display.manager import DisplayManager
    from modules.display.models import DisplayStatus, RefreshMode

    log.info("=== Part 1: real hardware - async mark_dirty + hash dedup ===")

    driver = Waveshare213gDriver(gpio_registry=GpioRegistry())
    manager = DisplayManager(driver, refresh_mode=RefreshMode.RESPONSIVE, debounce_seconds=1.0)
    manager.start()

    image = build_test_image(driver.capabilities, "Phase 3 / DisplayManager / PASS")

    t0 = time.monotonic()
    manager.mark_dirty(image)
    call_time = time.monotonic() - t0
    log.info("mark_dirty() returned in %.4fs (must not block on the real refresh)", call_time)
    if call_time > 0.5:
        log.error("FAIL: mark_dirty() took too long - it should return near-instantly")
        return False

    log.info("Waiting for the background worker to reach ONLINE...")
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if manager.status == DisplayStatus.ONLINE:
            break
        time.sleep(0.5)
    else:
        log.error("FAIL: manager never reached ONLINE within 60s, status=%s", manager.status)
        manager.stop()
        return False

    refresh_count_after_first = manager.stats.refresh_count
    log.info("First refresh done: status=%s stats=%s", manager.status, manager.stats.as_dict())

    log.info("Sending the identical frame again - should be deduped, no physical refresh...")
    manager.mark_dirty(image)
    time.sleep(3)  # RESPONSIVE debounce is 1s, give it time to (not) act
    refresh_count_after_dup = manager.stats.refresh_count
    manager.stop()

    if refresh_count_after_dup != refresh_count_after_first:
        log.error(
            "FAIL: refresh_count changed (%d -> %d) for an identical frame - hash dedup not working",
            refresh_count_after_first, refresh_count_after_dup,
        )
        return False

    log.info("PASS: identical frame correctly skipped (refresh_count stayed at %d)", refresh_count_after_dup)
    return True


class _FakeStuckDriver:
    """Minimal DisplayDriver stand-in whose render() never returns, to
    exercise the timeout/retry/cooldown path without real hardware or a
    real 75s wait. Duck-typed rather than subclassing DisplayDriver, since
    the manager only calls a handful of methods on it."""

    id = "fake_stuck"
    display_name = "Fake stuck display"
    device_type = "display"

    def __init__(self):
        self._started = False

    def detect(self) -> dict[str, Any] | None:
        return {"model": self.display_name}

    def start(self, **options: Any) -> bool:
        self._started = True
        return True

    def stop(self) -> None:
        self._started = False

    def get_status(self) -> dict[str, Any]:
        return {"ok": True, "started": self._started, "model": self.display_name}

    def render(self, image: Any, fast: bool = False) -> None:
        time.sleep(3600)  # never returns within any sane test timeout

    def clear(self) -> None:
        pass

    def sleep(self) -> None:
        pass


def part2_simulated_timeout() -> bool:
    from modules.display.manager import MAX_CONSECUTIVE_RETRIES, DisplayManager
    from modules.display.models import DisplayStatus, EventPriority

    log.info("=== Part 2: simulated stuck refresh -> Error, no infinite loop ===")

    driver = _FakeStuckDriver()
    manager = DisplayManager(driver, refresh_timeout=1.0)  # short timeout for a fast test
    manager.start()

    class _FakeImage:
        def tobytes(self):
            return b"fake"

    t0 = time.monotonic()
    manager.mark_dirty(_FakeImage(), priority=EventPriority.CRITICAL)  # bypass debounce

    expected_max_wait = 1.0 * (MAX_CONSECUTIVE_RETRIES + 1) + 5  # timeout * attempts + slack
    deadline = time.monotonic() + expected_max_wait
    reached_error = False
    while time.monotonic() < deadline:
        if manager.status == DisplayStatus.ERROR:
            reached_error = True
            break
        time.sleep(0.2)
    elapsed = time.monotonic() - t0
    final_status = manager.status  # read before stop() resets it to DISABLED
    manager.stop()

    if not reached_error:
        log.error("FAIL: manager never reached ERROR (status=%s) within %.1fs", final_status, expected_max_wait)
        return False

    log.info(
        "PASS: reached ERROR in %.1fs after %d retries, error_count=%d (no infinite loop)",
        elapsed, MAX_CONSECUTIVE_RETRIES + 1, manager.stats.error_count,
    )
    return True


class _FakeInstantDriver:
    """DisplayDriver stand-in whose render()/clear() succeed instantly -
    isolates the debounce *timing* invariant from real ~20s hardware
    refreshes."""

    id = "fake_instant"
    display_name = "Fake instant display"
    device_type = "display"

    def __init__(self):
        self._started = False

    def detect(self) -> dict[str, Any] | None:
        return {"model": self.display_name}

    def start(self, **options: Any) -> bool:
        self._started = True
        return True

    def stop(self) -> None:
        self._started = False

    def get_status(self) -> dict[str, Any]:
        return {"ok": True, "started": self._started, "model": self.display_name}

    def render(self, image: Any, fast: bool = False) -> None:
        pass

    def clear(self) -> None:
        pass

    def sleep(self) -> None:
        pass


def part3_debounce_survives_drifting_content() -> bool:
    """Regression test for a real bug found in Phase 5: a caller that
    polls faster than the debounce window and calls mark_dirty() on every
    poll - with content that keeps changing (simulating live telemetry
    like CPU% or "seconds since last RX" drifting almost every tick) -
    must still see the debounce window elapse and a refresh actually
    happen, not stall forever because the deadline kept getting reset."""
    from modules.display.manager import DisplayManager
    from modules.display.models import DisplayStatus

    log.info("=== Part 3: debounce survives continuously-drifting content ===")

    class _DriftingImage:
        def __init__(self, n):
            self._n = n

        def tobytes(self):
            return f"frame-{self._n}".encode()  # different every call, like real drifting telemetry

    driver = _FakeInstantDriver()
    debounce_seconds = 2.0
    manager = DisplayManager(driver, debounce_seconds=debounce_seconds)
    manager.start()

    poll_interval = 0.3
    n = 0
    t0 = time.monotonic()
    # Keep poking with different content faster than the debounce window,
    # for a bit longer than the window itself.
    while time.monotonic() - t0 < debounce_seconds + 1.0:
        manager.mark_dirty(_DriftingImage(n))
        n += 1
        time.sleep(poll_interval)

    # Give the worker a little slack beyond the debounce window to finish.
    deadline = time.monotonic() + debounce_seconds + 3.0
    reached_online = False
    while time.monotonic() < deadline:
        if manager.status == DisplayStatus.ONLINE:
            reached_online = True
            break
        time.sleep(0.1)

    refresh_count = manager.stats.refresh_count
    manager.stop()

    if not reached_online or refresh_count < 1:
        log.error(
            "FAIL: debounce window never elapsed under continuous drifting "
            "content (status reached ONLINE=%s, refresh_count=%d)",
            reached_online, refresh_count,
        )
        return False

    log.info("PASS: refresh happened (refresh_count=%d) despite continuous drifting content", refresh_count)
    return True


def main() -> int:
    ok1 = part1_real_hardware()
    ok2 = part2_simulated_timeout()
    ok3 = part3_debounce_survives_drifting_content()
    if ok1 and ok2 and ok3:
        log.info("ALL PASS")
        return 0
    log.error("FAILED: part1=%s part2=%s part3=%s", ok1, ok2, ok3)
    return 1


if __name__ == "__main__":
    sys.exit(main())
