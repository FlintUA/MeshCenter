"""Tests for meshsrv/radio_manager.py's RadioConnectionManager - H2-C
Phase 2 addition only: the on_reconnect hook, which lets reconnect() force
a fresh identity re-verification (the radio may have been swapped or
reconfigured while released to an external tool, which is the whole point
of the release - resuming should never silently trust whatever was true
before it)."""
import threading

from meshsrv.radio_manager import RadioConnectionManager


def _make_manager(**kwargs):
    defaults = dict(
        pause_event=threading.Event(),
        stop_listener=lambda: True,
        wait_serial_release=lambda **kw: True,
        serial_port="/dev/ttyACM0",
    )
    defaults.update(kwargs)
    return RadioConnectionManager(**defaults)


def test_reconnect_calls_on_reconnect_hook_before_clearing_pause():
    """Ordering matters: the hook must run while the listener is still
    paused, so a disconnect-recovery state it seeds is in place before
    the listener's own loop gets a chance to check it again."""
    pause_event = threading.Event()
    pause_event.set()
    observed_pause_state = []

    manager = _make_manager(
        pause_event=pause_event,
        on_reconnect=lambda: observed_pause_state.append(pause_event.is_set()),
    )
    manager._mode = "released"

    manager.reconnect()

    assert observed_pause_state == [True]
    assert pause_event.is_set() is False


def test_reconnect_works_without_an_on_reconnect_hook():
    """Default (no hook injected) must stay a harmless no-op - every
    existing caller that doesn't care about this still works unchanged."""
    manager = _make_manager()
    manager._mode = "released"

    ok, status = manager.reconnect()

    assert ok is True


def test_reconnect_tolerates_a_raising_on_reconnect_hook():
    """A broken hook must never prevent the actual reconnect from
    proceeding - pause_event still gets cleared."""
    pause_event = threading.Event()
    pause_event.set()

    def _raise():
        raise RuntimeError("identity check exploded")

    manager = _make_manager(pause_event=pause_event, on_reconnect=_raise)
    manager._mode = "released"

    ok, status = manager.reconnect()

    assert ok is True
    assert pause_event.is_set() is False


def test_reconnect_does_not_call_the_hook_when_already_connected():
    """reconnect() short-circuits to a no-op when already connected - the
    hook must not fire for a request that wasn't actually a transition."""
    calls = []
    manager = _make_manager(on_reconnect=lambda: calls.append(True))
    assert manager._mode == "connected"

    manager.reconnect()

    assert calls == []
