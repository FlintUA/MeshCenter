"""DisplayManager: the background worker that turns "here's what should be
on screen" requests into actual, debounced, non-blocking panel refreshes.
e-Paper Stage 1 plan, Phase 3 (sections 11, 22, 25-29, 42, 66-69).

Callers only ever interact with mark_dirty() (never blocks - pending-state,
not a frame queue, per section 42) and the status/stats properties. All
actual driver I/O happens on one background thread, matching the pattern
MeshCenter's other long-lived background threads already use (see
listen_meshtastic/telemetry_worker/radio_health_worker in server.py) rather
than introducing a new concurrency model.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

from modules.display.drivers.base import DisplayDriver
from modules.display.models import (
    DEFAULT_DEBOUNCE_SECONDS,
    DisplayStatus,
    EventPriority,
    RefreshMode,
    RefreshStats,
)

logger = logging.getLogger("display_manager")

REFRESH_TIMEOUT_SECONDS = 75.0  # plan section 27
MAX_CONSECUTIVE_RETRIES = 2  # plan section 28
ERROR_COOLDOWN_SECONDS = 60.0
_WORKER_POLL_SECONDS = 1.0


def _hash_image(image: Any) -> str:
    return hashlib.sha256(image.tobytes()).hexdigest()


def _run_with_timeout(fn, timeout: float) -> tuple[bool, Exception | None]:
    """Run fn() on a daemon thread, wait up to `timeout` seconds.

    Python has no way to forcibly cancel a running thread, so a genuine
    hang (e.g. BUSY never releasing) leaves that thread running in the
    background even after this returns (completed=False) - it's daemonized
    so it won't block process exit, and the caller is expected to treat a
    timeout as "this driver needs a fresh start() next time", not to keep
    waiting on the same attempt. This is what backs the "no infinite loop"
    requirement in plan section 27/28 given the vendor driver's own
    ReadBusy() has no timeout of its own (see
    drivers/vendor/waveshare_epd/epd2in13g_v2.py).
    """
    result: dict[str, Exception | None] = {}

    def _target():
        try:
            fn()
            result["error"] = None
        except Exception as exc:  # noqa: BLE001 - reported to caller, not swallowed
            result["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None
    return True, result.get("error")


class DisplayManager:
    def __init__(
        self,
        driver: DisplayDriver,
        refresh_mode: RefreshMode = RefreshMode.DEBOUNCE,
        debounce_seconds: float | None = None,
        refresh_timeout: float = REFRESH_TIMEOUT_SECONDS,
    ):
        self._driver = driver
        self._refresh_mode = refresh_mode
        self._debounce_seconds = (
            debounce_seconds
            if debounce_seconds is not None
            else DEFAULT_DEBOUNCE_SECONDS[refresh_mode]
        )
        self._refresh_timeout = refresh_timeout

        self._lock = threading.Lock()
        self._pending_image: Any = None
        self._pending_priority = EventPriority.NORMAL
        self._wake = threading.Event()
        self._stop_flag = threading.Event()
        self._worker: threading.Thread | None = None

        self._status = DisplayStatus.DISABLED
        self._stats = RefreshStats()
        self._last_hash: str | None = None
        self._consecutive_errors = 0
        self._error_cooldown_until: float = 0.0

    # -- public API ---------------------------------------------------

    @property
    def status(self) -> DisplayStatus:
        return self._status

    @property
    def stats(self) -> RefreshStats:
        return self._stats

    @property
    def capabilities(self):
        return self._driver.capabilities

    @property
    def display_name(self) -> str:
        return self._driver.display_name

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_flag.clear()
        self._status = DisplayStatus.CONFIGURED
        self._worker = threading.Thread(target=self._run, daemon=True, name="display-manager")
        self._worker.start()

    def stop(self) -> None:
        self._stop_flag.set()
        self._wake.set()
        if self._worker is not None:
            self._worker.join(timeout=5)
        self._driver.stop()
        self._status = DisplayStatus.DISABLED

    def mark_dirty(self, image: Any, priority: EventPriority = EventPriority.NORMAL) -> None:
        """Record the latest desired frame and wake the worker. Never
        blocks and never queues - a frame that arrives while an earlier
        one is still pending simply replaces it (plan section 42)."""
        with self._lock:
            self._pending_image = image
            if priority == EventPriority.CRITICAL:
                self._pending_priority = EventPriority.CRITICAL
        self._wake.set()

    def get_status_dict(self) -> dict[str, Any]:
        return {
            "status": self._status.value,
            "refresh_mode": self._refresh_mode.value,
            "debounce_seconds": self._debounce_seconds,
            **self._stats.as_dict(),
        }

    def set_refresh_mode(self, mode: RefreshMode, debounce_seconds: float | None = None) -> None:
        """Applied live - safe to change while the worker thread is
        running, unlike pins/SPI (see replace_driver()). A plain attribute
        write is enough here: Python's GIL makes it atomic, and the worker
        only ever reads self._refresh_mode/_debounce_seconds at the start
        of a debounce wait, never mutates them itself."""
        self._refresh_mode = mode
        self._debounce_seconds = (
            debounce_seconds if debounce_seconds is not None else DEFAULT_DEBOUNCE_SECONDS[mode]
        )

    def replace_driver(self, new_driver: DisplayDriver) -> None:
        """Swap the wrapped driver (e.g. after a GPIO/SPI/timeout config
        change) without tearing down the worker thread or losing
        debounce/stats state - only the physical-driver half of the
        manager changes. Stops the old driver first to release its
        GPIO/SPI claim before the new one can be started. Deliberately
        NOT part of the "applied live" settings path (set_refresh_mode) -
        a bad pin value here can reproduce the BUSY-hang debugging from
        Phases 1-2, so callers are expected to pair this with
        try_start_now() and only persist the new config if that succeeds
        (see api/api_hardware_display.py's re-init route)."""
        old_driver = self._driver
        try:
            old_driver.stop()
        except Exception:
            logger.exception("[EPAPER] replace_driver: failed to stop the old driver cleanly")
        self._driver = new_driver
        self._last_hash = None  # force a real refresh against the new driver next time
        self._status = DisplayStatus.CONFIGURED

    def try_start_now(self, timeout: float | None = None) -> tuple[bool, str | None]:
        """Synchronously attempt to start the current driver, bounded by a
        timeout. Only for the explicit GPIO/SPI re-init action - the
        normal async refresh pipeline always goes through _ensure_started()
        instead. Gives the caller an immediate pass/fail (e.g. "GPIO24
        conflicts" or a BUSY-timeout) instead of only discovering a bad
        pin config on the next debounced refresh, minutes later."""
        timeout = timeout if timeout is not None else self._refresh_timeout
        self._status = DisplayStatus.INITIALIZING

        def _start_or_raise():
            if not self._driver.start():
                raise RuntimeError(self._driver.get_status().get("error") or "start() returned False")

        completed, error = _run_with_timeout(_start_or_raise, timeout)
        if not completed:
            self._status = DisplayStatus.ERROR
            return False, f"start() timed out after {timeout:.0f}s"
        if error is not None:
            self._status = DisplayStatus.ERROR
            return False, str(error)
        self._status = DisplayStatus.CONFIGURED
        return True, None

    # -- worker thread --------------------------------------------------

    def _run(self) -> None:
        while not self._stop_flag.is_set():
            if not self._wake.wait(timeout=_WORKER_POLL_SECONDS):
                continue
            self._wake.clear()
            if self._stop_flag.is_set():
                break

            image, priority = self._take_pending()
            if image is None:
                continue

            if priority != EventPriority.CRITICAL:
                image, priority = self._debounce(image, priority)
                if self._stop_flag.is_set():
                    return
                if image is None:
                    continue

            self._refresh(image)

    def _take_pending(self) -> tuple[Any, EventPriority]:
        with self._lock:
            image, priority = self._pending_image, self._pending_priority
            self._pending_image, self._pending_priority = None, EventPriority.NORMAL
        return image, priority

    def _debounce(self, image: Any, priority: EventPriority) -> tuple[Any, EventPriority]:
        """Wait out the configured debounce window, merging in any newer
        frame that arrives during the wait (plan sections 66-69) and
        letting a CRITICAL event short-circuit the remaining wait.

        The deadline is pinned once, at the moment this pending item
        starts waiting - it is never pushed back out by a later
        mark_dirty(), whether or not that later frame's content differs.
        An earlier version of this method reset the deadline whenever the
        incoming frame's content differed from what it was already
        waiting on, which fixed the specific case of a caller re-sending
        an *identical* frame on every poll, but not the general case: real
        telemetry (CPU%, "seconds since last RX") drifts on nearly every
        tick, so "content differs" is true almost every poll on a live
        system too - the debounce window would still never elapse. Pinning
        the deadline at first arrival is correct regardless of how often
        or how substantively mark_dirty() is called before it fires."""
        deadline = time.monotonic() + self._debounce_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return image, priority
            if self._stop_flag.is_set():
                return None, priority
            if self._wake.wait(timeout=remaining):
                self._wake.clear()
                newer_image, newer_priority = self._take_pending()
                if newer_image is None:
                    continue
                if newer_priority == EventPriority.CRITICAL:
                    return newer_image, newer_priority
                image, priority = newer_image, newer_priority

    def _refresh(self, image: Any) -> None:
        if time.monotonic() < self._error_cooldown_until:
            logger.debug("[EPAPER] Skipping refresh - still in post-error cooldown")
            return

        # Plan section 44: missing hardware (no /dev/spidevN.M) should be
        # a distinct, graceful UNAVAILABLE - not treated as a fault that
        # retries 3x and enters a 60s error cooldown loop forever. detect()
        # is a cheap path check (see Waveshare213gDriver.detect()), safe to
        # call on every refresh attempt without a timeout wrapper.
        if self._driver.detect() is None:
            if self._status != DisplayStatus.UNAVAILABLE:
                logger.warning("[EPAPER] Display hardware not detected - marking UNAVAILABLE")
            self._status = DisplayStatus.UNAVAILABLE
            return

        image_hash = _hash_image(image)
        if image_hash == self._last_hash:
            logger.debug("[EPAPER] Frame unchanged since last refresh, skipping physical refresh")
            return

        self._status = DisplayStatus.REFRESHING
        t0 = time.monotonic()
        ok = self._render_with_retries(image)
        elapsed = time.monotonic() - t0

        if ok:
            self._last_hash = image_hash
            self._consecutive_errors = 0
            self._status = DisplayStatus.ONLINE
            self._stats.record_success(elapsed)
        else:
            self._consecutive_errors += 1
            self._stats.record_error()
            self._status = DisplayStatus.ERROR
            if self._consecutive_errors >= MAX_CONSECUTIVE_RETRIES + 1:
                self._error_cooldown_until = time.monotonic() + ERROR_COOLDOWN_SECONDS
                logger.error(
                    "[EPAPER] Display refresh failed %d times in a row, entering %.0fs cooldown",
                    self._consecutive_errors,
                    ERROR_COOLDOWN_SECONDS,
                )

    def _render_with_retries(self, image: Any) -> bool:
        for attempt in range(MAX_CONSECUTIVE_RETRIES + 1):
            if not self._ensure_started(attempt):
                continue
            completed, error = _run_with_timeout(
                lambda: self._driver.render(image), self._refresh_timeout
            )
            if completed and error is None:
                return True
            if not completed:
                logger.error(
                    "[EPAPER] Display refresh timed out after %.0fs (attempt %d/%d) - "
                    "treating driver as needing a fresh start()",
                    self._refresh_timeout,
                    attempt + 1,
                    MAX_CONSECUTIVE_RETRIES + 1,
                )
                # Abandon this driver session - the vendor code offers no
                # way to interrupt a stuck BUSY poll, so the next attempt
                # gets a brand new start() rather than reusing possibly
                # wedged SPI/GPIO state.
                self._driver.stop()
            else:
                logger.error(
                    "[EPAPER] Display refresh raised %r (attempt %d/%d)",
                    error, attempt + 1, MAX_CONSECUTIVE_RETRIES + 1,
                )
        return False

    def _ensure_started(self, attempt: int) -> bool:
        """Like render(), start() ultimately runs the vendor driver's
        init()/ReadBusy() and can hang exactly the same way a stuck BUSY
        line would hang a refresh - confirmed live: an unprotected
        synchronous self._driver.start() call here left the worker thread
        blocked indefinitely with status stuck at INITIALIZING and no
        error ever logged, since the call simply never returned. Routing
        it through the same _run_with_timeout watchdog as render() means a
        hung start() is treated as a failed attempt (subject to the same
        MAX_CONSECUTIVE_RETRIES + cooldown) instead of freezing the entire
        manager forever."""
        status = self._driver.get_status()
        if status.get("started"):
            return True
        self._status = DisplayStatus.INITIALIZING

        def _start_or_raise():
            # _run_with_timeout only detects failure via a raised
            # exception, but driver.start() reports failure by returning
            # False (see Waveshare213gDriver.start()) - convert that into
            # a raise so a plain "start() returned False" doesn't look
            # like success to the watchdog.
            if not self._driver.start():
                raise RuntimeError(self._driver.get_status().get("error") or "start() returned False")

        completed, error = _run_with_timeout(_start_or_raise, self._refresh_timeout)
        if not completed:
            logger.error(
                "[EPAPER] Display start() timed out after %.0fs (attempt %d/%d)",
                self._refresh_timeout, attempt + 1, MAX_CONSECUTIVE_RETRIES + 1,
            )
            return False
        if error is not None:
            logger.error(
                "[EPAPER] Display start() raised %r (attempt %d/%d)",
                error, attempt + 1, MAX_CONSECUTIVE_RETRIES + 1,
            )
            return False
        return True
