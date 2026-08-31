"""Tests for meshsrv/installation_time_assignment.py (Stage C). No real
sleeping anywhere - sleep_fn/monotonic_fn are injected fakes, per this
project's established discipline for time-based logic (see
tests/test_radio_session_timeout.py and friends).
"""

from unittest.mock import patch

import pytest

from meshsrv.installation_time_assignment import assign_installation_time_when_confirmed
from meshsrv.instance_manager import InstanceManager


class _FakeClock:
    """A monotonic_fn/sleep_fn pair where sleep_fn actually advances the
    fake clock (instead of doing nothing), so the deadline-comparison logic
    in assign_installation_time_when_confirmed() is exercised for real,
    just without any wall-clock delay."""

    def __init__(self):
        self.now = 0.0
        self.sleep_calls = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        self.now += seconds


@pytest.fixture
def manager(tmp_path):
    m = InstanceManager(tmp_path / "instance.json")
    m.load_or_create({})
    return m


def test_confirmed_on_first_poll_saves_and_returns_true(manager):
    clock = _FakeClock()
    with patch(
        "meshsrv.installation_time_assignment.get_confirmed_utc_time",
        return_value="2026-09-01T07:00:00+00:00",
    ):
        result = assign_installation_time_when_confirmed(
            manager, poll_interval=15, timeout=300,
            sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
        )

    assert result is True
    assert clock.sleep_calls == []
    identity = manager.get()
    assert identity["installation"]["assigned_at"] == "2026-09-01T07:00:00+00:00"
    assert identity["installation"]["time_source"] == "system_ntp"


def test_confirmed_on_a_later_poll_sleeps_the_expected_number_of_times(manager):
    clock = _FakeClock()
    responses = [None, None, "2026-09-01T07:00:00+00:00"]
    with patch(
        "meshsrv.installation_time_assignment.get_confirmed_utc_time",
        side_effect=responses,
    ):
        result = assign_installation_time_when_confirmed(
            manager, poll_interval=15, timeout=300,
            sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
        )

    assert result is True
    assert clock.sleep_calls == [15, 15]
    assert manager.get()["installation"]["time_source"] == "system_ntp"


def test_never_confirmed_gives_up_after_timeout_without_saving(manager):
    clock = _FakeClock()
    original_id = manager.get()["installation"]["id"]

    with patch(
        "meshsrv.installation_time_assignment.get_confirmed_utc_time",
        return_value=None,
    ):
        result = assign_installation_time_when_confirmed(
            manager, poll_interval=15, timeout=300,
            sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
        )

    assert result is False
    # Loop must actually stop, not poll forever: timeout // poll_interval.
    assert len(clock.sleep_calls) == 300 // 15
    identity = manager.get()
    assert identity["installation"]["time_source"] == "pending"
    assert identity["installation"]["assigned_at"] is None
    assert identity["installation"]["id"] == original_id


def test_successful_save_preserves_every_other_existing_field(manager):
    identity_before = manager.get()
    updated = dict(identity_before)
    updated["active_profile_id"] = "067a40fa"
    updated["radio"] = dict(identity_before["radio"])
    updated["radio"]["node_id"] = "!067a40fa"
    manager.save(updated)

    clock = _FakeClock()
    with patch(
        "meshsrv.installation_time_assignment.get_confirmed_utc_time",
        return_value="2026-09-01T07:00:00+00:00",
    ):
        assign_installation_time_when_confirmed(
            manager, poll_interval=15, timeout=300,
            sleep_fn=clock.sleep, monotonic_fn=clock.monotonic,
        )

    identity = manager.get()
    assert identity["active_profile_id"] == "067a40fa"
    assert identity["radio"]["node_id"] == "!067a40fa"
    assert identity["installation"]["id"] == identity_before["installation"]["id"]
    assert identity["installation"]["assignment_reason"] == identity_before["installation"]["assignment_reason"]
