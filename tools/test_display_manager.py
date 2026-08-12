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
import threading
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


class _FakeGatedDriver:
    """DisplayDriver stand-in that (a) can hold start() open on a
    controllable gate, so a test can deterministically catch the worker
    thread *inside* start() rather than hoping a sleep() wins a timing
    race, and (b) tracks - across all instances, via a class-level lock -
    whether two instances' start()/render()/stop() are ever "active"
    (entered but not yet returned) at the same time. That's the exact
    failure mode swap_driver_and_start() exists to prevent (see
    manager.py): the vendor Waveshare wrapper monkey-patches module-level
    shared state, so two instances' start()/render() running concurrently
    corrupts each other regardless of which instances they are - this
    check doesn't care about identity, only about overlap."""

    _active_lock = threading.Lock()
    _active: str | None = None
    violations: list[str] = []

    def __init__(self, name: str, start_gate: threading.Event | None = None):
        self.name = name
        self.display_name = name
        self.id = name
        self._started = False
        self._start_gate = start_gate
        self.entered_start = threading.Event()
        self.start_returned = threading.Event()
        self.stop_called = threading.Event()

    def _enter(self, where: str) -> None:
        with _FakeGatedDriver._active_lock:
            if _FakeGatedDriver._active is not None:
                _FakeGatedDriver.violations.append(
                    f"{self.name}.{where}() entered while {_FakeGatedDriver._active} was still active"
                )
            _FakeGatedDriver._active = f"{self.name}.{where}"

    def _exit(self) -> None:
        with _FakeGatedDriver._active_lock:
            _FakeGatedDriver._active = None

    def detect(self) -> dict[str, Any] | None:
        return {"model": self.name}

    def start(self, **options: Any) -> bool:
        self._enter("start")
        try:
            self.entered_start.set()
            if self._start_gate is not None:
                self._start_gate.wait(timeout=5)
            self._started = True
            return True
        finally:
            self.start_returned.set()
            self._exit()

    def stop(self) -> None:
        self.stop_called.set()
        self._enter("stop")
        try:
            self._started = False
        finally:
            self._exit()

    def get_status(self) -> dict[str, Any]:
        return {"ok": True, "started": self._started, "model": self.name}

    def render(self, image: Any, fast: bool = False) -> None:
        self._enter("render")
        self._exit()

    def clear(self) -> None:
        pass

    def sleep(self) -> None:
        pass


class _FakeImage:
    def tobytes(self):
        return b"fake"


def part4_reinit_does_not_race_worker() -> bool:
    """Regression test for a real race found live (2026-08-12): the worker
    thread and swap_driver_and_start() (called from a Flask request thread
    via /reinit) could both call start()/render() on the same driver
    instance at once, since nothing paused the worker during a reinit.
    Observed live as a start() timeout immediately followed by
    GPIODeviceClosed('Button is closed or uninitialized').

    Deterministic by construction, not by luck: driver_a's start() is held
    open on a gate until the test has *confirmed* (via entered_start, not
    a sleep) that the worker is genuinely inside it, then confirms the
    concurrently-started reinit is genuinely still blocked (driver_a not
    yet stopped, driver_b not yet started) before releasing the gate -
    matching the lesson from the GpioRegistry HTTP-test mistake: a test
    that can't distinguish bug-present from bug-fixed proves nothing."""
    from modules.display.manager import DisplayManager
    from modules.display.models import EventPriority

    log.info("=== Part 4: swap_driver_and_start() never overlaps the worker on one driver ===")

    _FakeGatedDriver.violations.clear()
    _FakeGatedDriver._active = None

    start_gate = threading.Event()  # held closed - driver_a's start() blocks here
    driver_a = _FakeGatedDriver("driver_a", start_gate=start_gate)
    manager = DisplayManager(driver_a, refresh_timeout=5.0)
    manager.start()

    manager.mark_dirty(_FakeImage(), priority=EventPriority.CRITICAL)  # bypass debounce
    if not driver_a.entered_start.wait(timeout=5):
        manager.stop()
        log.error("FAIL: worker never reached driver_a.start() - test setup broken")
        return False

    driver_b = _FakeGatedDriver("driver_b")
    reinit_result: dict[str, Any] = {}

    def _do_reinit():
        ok, error = manager.swap_driver_and_start(driver_b, timeout=5.0)
        reinit_result["ok"] = ok
        reinit_result["error"] = error

    reinit_thread = threading.Thread(target=_do_reinit, daemon=True)
    reinit_thread.start()

    # Prove the reinit is genuinely still blocked - not "probably" via a
    # bigger sleep, but by checking the actual state it must not have
    # reached yet: driver_a hasn't been stopped, driver_b hasn't started.
    time.sleep(0.5)
    if driver_a.stop_called.is_set() or driver_b.entered_start.is_set():
        manager.stop()
        log.error(
            "FAIL: swap_driver_and_start() proceeded (driver_a stopped=%s, "
            "driver_b started=%s) while driver_a.start() was still running",
            driver_a.stop_called.is_set(), driver_b.entered_start.is_set(),
        )
        return False

    # Now let driver_a.start() finish - the reinit should unblock and
    # proceed in order: driver_a.start() returns, THEN driver_a.stop(),
    # THEN driver_b.start().
    start_gate.set()
    reinit_thread.join(timeout=10)
    manager.stop()

    if reinit_thread.is_alive():
        log.error("FAIL: swap_driver_and_start() never returned")
        return False
    if not reinit_result.get("ok"):
        log.error("FAIL: swap_driver_and_start() reported failure: %s", reinit_result.get("error"))
        return False
    if not driver_a.start_returned.is_set() or not driver_a.stop_called.is_set():
        log.error("FAIL: expected driver_a.start() to return and driver_a.stop() to be called")
        return False
    if not driver_b.entered_start.is_set():
        log.error("FAIL: driver_b.start() was never called")
        return False
    if _FakeGatedDriver.violations:
        log.error("FAIL: overlapping driver method calls detected: %s", _FakeGatedDriver.violations)
        return False

    log.info("PASS: reinit correctly waited for the in-flight worker cycle, no overlapping driver calls")
    return True


def main() -> int:
    ok1 = part1_real_hardware()
    ok2 = part2_simulated_timeout()
    ok3 = part3_debounce_survives_drifting_content()
    ok4 = part4_reinit_does_not_race_worker()
    if ok1 and ok2 and ok3 and ok4:
        log.info("ALL PASS")
        return 0
    log.error("FAILED: part1=%s part2=%s part3=%s part4=%s", ok1, ok2, ok3, ok4)
    return 1


if __name__ == "__main__":
    sys.exit(main())
