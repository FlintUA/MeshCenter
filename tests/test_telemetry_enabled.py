"""H1-B1 (F5): telemetry_config["enabled"] was written by
/api/telemetry/config but never read anywhere - toggling it had no effect
on anything. Agreed semantics (Flint): disabling only stops WRITING
HISTORY - live values/cards/radio health (server.py's
apply_telemetry_values()/apply_node_telemetry(), which update
telemetry_current/sensor_data/base_status/node fields directly,
independently of these two functions) keep updating regardless.
"""

import threading

import pytest


@pytest.fixture
def telemetry_module(server_module, tmp_path, monkeypatch):
    """Same fixture shape as tests/test_telemetry_locking.py - see that
    file's own docstring for why each of these monkeypatches exists."""
    import telemetry.telemetry as module

    module.configure_storage(str(tmp_path / "telemetry_history.json"))
    monkeypatch.setattr(module, "telemetry_history", [])
    monkeypatch.setattr(module, "telemetry_config", {"interval": 300, "enabled": True})
    monkeypatch.setattr(module, "telemetry_last_save_time", 0)
    monkeypatch.setattr(module, "_telemetry_lock", threading.RLock())
    return module


def test_disabled_add_telemetry_record_does_not_append(telemetry_module):
    telemetry_module.telemetry_config["enabled"] = False

    saved = telemetry_module.add_telemetry_record(temp=20.0, humidity=50.0, pressure=1013.0, voltage=4.0, current=100.0)

    assert saved is False
    assert telemetry_module.telemetry_history == []


def test_disabled_add_node_telemetry_record_does_not_append(telemetry_module):
    telemetry_module.telemetry_config["enabled"] = False

    saved = telemetry_module.add_node_telemetry_record("!11223344", {"voltage": 4.0, "battery_level": 80})

    assert saved is False
    assert telemetry_module.telemetry_history == []


def test_re_enabling_resumes_history_without_an_extra_interval_wait(telemetry_module):
    """Disabling must not somehow make the FIRST record after re-enabling
    wait out a longer window than normal - the rate limit is purely based
    on elapsed real time since the last actual record (there wasn't one
    here), not on some accumulated "disabled" penalty."""
    telemetry_module.telemetry_config["enabled"] = False

    # Several calls while disabled - all rejected, nothing appended, and
    # critically nothing here should be setting up a false rate-limit
    # state for later.
    for _ in range(3):
        assert telemetry_module.add_telemetry_record(temp=20.0, humidity=None, pressure=None, voltage=None, current=None) is False
    assert telemetry_module.telemetry_history == []

    telemetry_module.telemetry_config["enabled"] = True

    # Immediately after re-enabling - no prior successful record exists,
    # so this must succeed right away, not be blocked for a full interval.
    saved = telemetry_module.add_telemetry_record(temp=21.0, humidity=None, pressure=None, voltage=None, current=None)
    assert saved is True
    assert len(telemetry_module.telemetry_history) == 1


def test_re_enabling_resumes_node_history_without_an_extra_interval_wait(telemetry_module):
    telemetry_module.telemetry_config["enabled"] = False

    for _ in range(3):
        assert telemetry_module.add_node_telemetry_record("!11223344", {"voltage": 4.0}) is False
    assert telemetry_module.telemetry_history == []

    telemetry_module.telemetry_config["enabled"] = True

    saved = telemetry_module.add_node_telemetry_record("!11223344", {"voltage": 4.0})
    assert saved is True
    assert len(telemetry_module.telemetry_history) == 1


def test_enabled_defaults_to_true_when_config_key_missing(telemetry_module):
    """An old telemetry_history.json written before this field existed (or
    a config dict that simply omits it) must behave as enabled, not
    silently stop recording history."""
    del telemetry_module.telemetry_config["enabled"]

    saved = telemetry_module.add_telemetry_record(temp=20.0, humidity=None, pressure=None, voltage=None, current=None)

    assert saved is True
    assert len(telemetry_module.telemetry_history) == 1
