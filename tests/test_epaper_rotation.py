"""Tests for task 40's auto-rotation: the pure advance_rotation() timing
function (modules/display/service.py), plus a regression check that
_poll_once()'s critical-alert path never touches ui_state's rotation
bookkeeping (the "pause is free" design - see that function's own comment
at the call site).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from modules.display.drivers.base import DisplayCapabilities
from modules.display.models import EventPriority
from modules.display.service import _ContentState, _poll_once, advance_rotation

WEACT_CAPS = DisplayCapabilities(width=200, height=200, colors=("black", "white"))


# ---------------- advance_rotation() ----------------

def test_advances_when_interval_elapsed():
    # last advance at t=0, now=30, interval=30 -> exactly due.
    new_index, new_ts, advanced = advance_rotation(0, 3, last_advance_ts=0.0, now=30.0, interval_seconds=30.0)
    assert advanced is True
    assert new_index == 1
    assert new_ts == 30.0


def test_does_not_advance_before_interval_elapsed():
    new_index, new_ts, advanced = advance_rotation(0, 3, last_advance_ts=0.0, now=10.0, interval_seconds=30.0)
    assert advanced is False
    assert new_index == 0
    assert new_ts == 0.0  # unchanged


def test_wraparound_last_page_to_first():
    new_index, new_ts, advanced = advance_rotation(2, 3, last_advance_ts=0.0, now=30.0, interval_seconds=30.0)
    assert advanced is True
    assert new_index == 0


def test_single_page_keeps_looping_same_index():
    new_index, new_ts, advanced = advance_rotation(0, 1, last_advance_ts=0.0, now=30.0, interval_seconds=30.0)
    assert advanced is True
    assert new_index == 0  # (0+1) % 1 == 0 - "loops" the one page, doesn't crash


def test_empty_page_list_never_advances():
    new_index, new_ts, advanced = advance_rotation(0, 0, last_advance_ts=0.0, now=999.0, interval_seconds=30.0)
    assert advanced is False
    assert new_index == 0
    assert new_ts == 0.0


def test_empty_page_list_with_no_prior_timestamp_does_not_crash():
    new_index, new_ts, advanced = advance_rotation(5, 0, last_advance_ts=None, now=100.0, interval_seconds=30.0)
    assert advanced is False
    assert new_index == 0
    assert new_ts == 100.0


def test_first_ever_tick_advances_immediately_at_current_index():
    # last_advance_ts=None means rotation was just turned on (or a fresh
    # ui_state after a restart) - the doc's own design: show the current
    # index right away, don't make the user wait a full interval first.
    new_index, new_ts, advanced = advance_rotation(2, 4, last_advance_ts=None, now=1000.0, interval_seconds=30.0)
    assert advanced is True
    assert new_index == 2  # stays at the given index, doesn't jump to 3
    assert new_ts == 1000.0


def test_stale_index_from_shrunk_page_list_wraps_safely():
    # index=5 left over from when there were 6 pages checked; only 2 are
    # checked now - must not raise or return an out-of-range index.
    new_index, new_ts, advanced = advance_rotation(5, 2, last_advance_ts=0.0, now=30.0, interval_seconds=30.0)
    assert 0 <= new_index < 2


# ---------------- _poll_once(): critical alert pauses rotation for free ----------------

def _rotation_poll_kwargs(ui_state, rotation_config):
    # get_active_page reads from the same ui_state dict rotation writes
    # to - matching server.py's real wiring (_epaper_get_active_page()
    # reads epaper_ui_state, the same dict object passed as ui_state=
    # here), so a rotation-driven active_page update is actually visible
    # to the render step within the same poll tick, not just to a test
    # double that doesn't reflect it.
    return dict(
        get_battery_percent=lambda: None,
        get_active_page=lambda: ui_state.get("active_page", "status"),
        get_last_error=lambda: "",
        get_power_readings=lambda: {},
        get_cpu_temp=lambda: None,
        get_latest_message=lambda: None,
        get_rotation_config=lambda: rotation_config,
        ui_state=ui_state,
    )


def test_critical_alert_does_not_advance_rotation_bookkeeping():
    manager = MagicMock()
    manager.capabilities = WEACT_CAPS
    ui_state = {"active_page": "status", "rotation_index": 0, "rotation_last_advance_ts": 0.0}
    rotation_config = {"enabled": True, "pages": ["status", "radio"], "interval_seconds": 5.0}

    # radio_status "offline" (mode="error") triggers the critical Alert
    # Screen path, which returns before rotation logic ever runs.
    _poll_once(
        manager, MagicMock(), {},
        get_radio_status=lambda: {"mode": "error", "serial_port": "/dev/ttyACM0"},
        get_cpu_percent=lambda: None,
        get_ram_percent=lambda: None,
        get_listener_alive=lambda: True,
        local_node_name="Test Node",
        content_state=_ContentState(),
        **_rotation_poll_kwargs(ui_state, rotation_config),
    )

    assert ui_state["rotation_index"] == 0
    assert ui_state["rotation_last_advance_ts"] == 0.0
    assert ui_state["active_page"] == "status"  # untouched by rotation
    manager.mark_dirty.assert_called_once()


def test_normal_poll_with_rotation_enabled_advances_active_page():
    manager = MagicMock()
    manager.capabilities = WEACT_CAPS
    ui_state = {"active_page": "status", "rotation_index": 0, "rotation_last_advance_ts": 0.0}
    rotation_config = {"enabled": True, "pages": ["status", "radio"], "interval_seconds": 5.0}

    _poll_once(
        manager, MagicMock(), {},
        get_radio_status=lambda: {"mode": "connected", "serial_port": "/dev/ttyACM0"},
        get_cpu_percent=lambda: 10.0,
        get_ram_percent=lambda: 20.0,
        get_listener_alive=lambda: True,
        local_node_name="Test Node",
        content_state=_ContentState(),
        **_rotation_poll_kwargs(ui_state, rotation_config),
    )

    # last_advance_ts=0.0 is far in the past relative to real time.time()
    # used internally, so this tick should have advanced.
    assert ui_state["rotation_index"] == 1
    assert ui_state["active_page"] == "radio"
    # A genuine rotation advance must bypass debounce (CRITICAL) - see
    # test_rotation_advance_bypasses_debounce_via_critical_priority()
    # below for why (DisplayManager._debounce() silently drops a page
    # whose mark_dirty() call gets superseded by the next tick's newer
    # frame before its debounce window elapses).
    _, kwargs = manager.mark_dirty.call_args
    assert kwargs["priority"] == EventPriority.CRITICAL


def test_rotation_disabled_leaves_active_page_alone():
    manager = MagicMock()
    manager.capabilities = WEACT_CAPS
    ui_state = {"active_page": "power"}
    rotation_config = {"enabled": False, "pages": ["status", "radio"], "interval_seconds": 5.0}

    _poll_once(
        manager, MagicMock(), {},
        get_radio_status=lambda: {"mode": "connected", "serial_port": "/dev/ttyACM0"},
        get_cpu_percent=lambda: 10.0,
        get_ram_percent=lambda: 20.0,
        get_listener_alive=lambda: True,
        local_node_name="Test Node",
        content_state=_ContentState(),
        **_rotation_poll_kwargs(ui_state, rotation_config),
    )

    assert "rotation_index" not in ui_state
    assert ui_state["active_page"] == "power"
    # Not a rotation advance (rotation is disabled) - stays NORMAL, so
    # routine (non-rotation) content still respects debounce_seconds as
    # before.
    _, kwargs = manager.mark_dirty.call_args
    assert kwargs["priority"] == EventPriority.NORMAL


def test_rotation_advance_bypasses_debounce_via_critical_priority():
    """The actual bug this was written for (live report: "only Radio and
    System show up" with all 4 pages checked and rotation_interval_seconds
    (5s) shorter than debounce_seconds (6s)). DisplayManager._debounce()
    silently *replaces* a still-waiting NORMAL-priority frame with a newer
    one that arrives before its debounce window elapses - the first page
    is never shown at all. CRITICAL priority skips that wait entirely
    (see manager.py's _run(): "if priority != EventPriority.CRITICAL").
    Every rotation-driven page change must use it, or fast rotation
    intervals silently drop pages from the cycle."""
    manager = MagicMock()
    manager.capabilities = WEACT_CAPS
    ui_state = {"active_page": "status", "rotation_index": 0, "rotation_last_advance_ts": None}
    rotation_config = {
        "enabled": True, "pages": ["status", "radio", "power", "system"], "interval_seconds": 5.0,
    }
    # Simulate 5 consecutive rotation ticks (one full cycle plus one) -
    # every single one must mark_dirty() with CRITICAL, not just the
    # first, since a shorter-than-debounce interval affects every tick
    # equally, not just the initial one.
    for _ in range(5):
        ui_state["rotation_last_advance_ts"] = None  # force "advanced" every call, isolating the assertion
        _poll_once(
            manager, MagicMock(), {},
            get_radio_status=lambda: {"mode": "connected", "serial_port": "/dev/ttyACM0"},
            get_cpu_percent=lambda: 10.0,
            get_ram_percent=lambda: 20.0,
            get_listener_alive=lambda: True,
            local_node_name="Test Node",
            content_state=_ContentState(),
            **_rotation_poll_kwargs(ui_state, rotation_config),
        )
        _, kwargs = manager.mark_dirty.call_args
        assert kwargs["priority"] == EventPriority.CRITICAL, "every rotation-driven page must bypass debounce"
