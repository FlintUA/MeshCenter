"""Unit test (no hardware) for waveshare_213g.py's _VENDOR_LOCK: proves the
bounded-acquire behavior added to close the second race found in
DisplayManager's retry loop (see manager.py's module docstring and
_render_with_retries()) - an abandoned watchdog thread from a timed-out
start() could still be touching epdconfig.implementation (module-level
shared state) when a later attempt calls start() again on the same
instance.

Pure lock-logic test, deterministic by construction (holds the lock from
the main thread, forcing a background caller to genuinely contend for it -
not hoping a sleep() wins a timing race), so this runs anywhere with the
venv active, no dev-node SSH or real panel needed:

    python3 tools/test_waveshare_vendor_lock.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_bounded_acquire_fails_fast_and_distinctly() -> bool:
    import modules.display.drivers.waveshare_213g as w

    log_prefix = "test_bounded_acquire_fails_fast_and_distinctly"

    # Shrink the timeout for a fast test - _vendor_session() reads the
    # module global at call time, so patching it here is enough.
    original_timeout = w._VENDOR_LOCK_TIMEOUT
    w._VENDOR_LOCK_TIMEOUT = 0.5

    try:
        w._VENDOR_LOCK.acquire()  # hold it from the main thread
        try:
            t0 = time.monotonic()
            raised = None
            try:
                with w._vendor_session():
                    pass  # should never reach here
            except Exception as exc:  # noqa: BLE001 - inspecting the type is the point
                raised = exc
            elapsed = time.monotonic() - t0

            if not isinstance(raised, w.VendorSessionLocked):
                print(f"FAIL [{log_prefix}]: expected VendorSessionLocked, got {raised!r}")
                return False
            if "lock contention" not in str(raised) or "BUSY" not in str(raised):
                print(
                    f"FAIL [{log_prefix}]: message not distinctly worded "
                    f"(must mention lock contention AND explicitly rule out "
                    f"a hardware BUSY timeout) - got: {raised}"
                )
                return False
            if elapsed > 2.0:
                print(f"FAIL [{log_prefix}]: took {elapsed:.2f}s, expected ~0.5s bounded wait")
                return False
        finally:
            w._VENDOR_LOCK.release()
    finally:
        w._VENDOR_LOCK_TIMEOUT = original_timeout

    print(f"PASS [{log_prefix}]: raised VendorSessionLocked with a distinct "
          f"message in {elapsed:.2f}s, did not hang")
    return True


def test_stop_never_raises_on_lock_contention() -> bool:
    import modules.display.drivers.waveshare_213g as w
    from modules.display.gpio_registry import GpioRegistry

    log_prefix = "test_stop_never_raises_on_lock_contention"

    original_timeout = w._VENDOR_LOCK_TIMEOUT
    w._VENDOR_LOCK_TIMEOUT = 0.3

    registry = GpioRegistry()
    driver = w.Waveshare213gDriver(gpio_registry=registry)
    # Simulate "already started" without touching real hardware - stop()
    # only needs self._started/_epdconfig truthy to attempt module_exit().
    driver._started = True
    driver._epdconfig = object()
    registry.claim(driver._pins, owner=driver.id)

    try:
        w._VENDOR_LOCK.acquire()  # force stop()'s _vendor_session() to contend
        try:
            try:
                driver.stop()
            except Exception as exc:  # noqa: BLE001 - the point is that this must NOT happen
                print(f"FAIL [{log_prefix}]: stop() raised {exc!r} - must never raise")
                return False
        finally:
            w._VENDOR_LOCK.release()
    finally:
        w._VENDOR_LOCK_TIMEOUT = original_timeout

    if registry.get_owner(driver._pins["rst"]) is not None:
        print(f"FAIL [{log_prefix}]: GPIO registry claim was not released despite the lock failure")
        return False
    if driver._started:
        print(f"FAIL [{log_prefix}]: driver still reports _started=True after stop()")
        return False

    print(f"PASS [{log_prefix}]: stop() swallowed the lock-contention failure, "
          f"still released GPIO registry claim and reset state")
    return True


def test_lock_releases_normally_after_contention_clears() -> bool:
    """Confirms the lock isn't left in a bad state by the above - a normal
    acquire succeeds immediately once nothing is holding it."""
    import modules.display.drivers.waveshare_213g as w

    log_prefix = "test_lock_releases_normally_after_contention_clears"

    t0 = time.monotonic()
    with w._vendor_session():
        pass
    elapsed = time.monotonic() - t0

    if elapsed > 1.0:
        print(f"FAIL [{log_prefix}]: uncontended acquire took {elapsed:.2f}s, expected near-instant")
        return False

    print(f"PASS [{log_prefix}]: uncontended _vendor_session() acquired/released in {elapsed:.3f}s")
    return True


def main() -> int:
    results = [
        test_bounded_acquire_fails_fast_and_distinctly(),
        test_stop_never_raises_on_lock_contention(),
        test_lock_releases_normally_after_contention_clears(),
    ]
    if all(results):
        print("ALL PASS")
        return 0
    print("FAILED")
    return 1


if __name__ == "__main__":
    sys.exit(main())
