"""Tests for WHICH background workers start_runtime() starts, per transport.

Root cause this exists for: radio_health_worker() sat inside a serial-only
gate in start_runtime(), so #286 (transport-aware status) and #288
(auto-reconnect) were dead code in production for every TCP/Bluetooth radio -
live-caught on pixel-111 (zero "Auto-Reconnect enabled" events, System page
stuck on the initializer's "STARTING", no recovery after a reset). Their own
tests called compute_radio_health_status()/process_transport_autorecovery()
directly, so nothing ever exercised the gate. These tests run the real
start_runtime() with everything heavy neutralized and only thread starts
recorded, so a wrong gate condition fails here.
"""
import types

import pytest

SERIAL_ONLY_WORKERS = {
    "listen_meshtastic",
    "cleanup_seen_ids",
    "telemetry_worker",
    "telemetry_buffer_worker",
    "ack_timeout_worker",
}


class _RecordingThread:
    started = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self._target = target

    def start(self):
        _RecordingThread.started.append(getattr(self._target, "__name__", repr(self._target)))


def _radio(transport):
    if transport == "tcp":
        return {"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}
    if transport == "bluetooth":
        return {"node_id": "!756f9960", "transport": "bluetooth", "endpoint": {"address": "3C:DC:75:6F:99:61"}}
    return {"node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}}


@pytest.fixture
def run_start_runtime(server_module, monkeypatch):
    """Returns f(transport, identity_status) -> (started worker names,
    restore_active_transport calls)."""
    original_pause = server_module.pause_listen.is_set()

    def _run(transport, identity_status="MATCH"):
        _RecordingThread.started = []
        restore_calls = []

        identity = dict(server_module.INSTANCE_IDENTITY)
        identity["radio"] = _radio(transport)
        monkeypatch.setattr(server_module, "INSTANCE_IDENTITY", identity)
        monkeypatch.setattr(
            server_module, "RADIO_IDENTITY_RESULT", {"status": identity_status, "detected": {}, "error": None}
        )

        monkeypatch.setattr(server_module, "_runtime_started", False)
        monkeypatch.setattr(server_module, "_acquire_runtime_lock", lambda: None)
        monkeypatch.setattr(server_module, "verify_radio_identity", lambda: "")
        for name in (
            "load_messages", "reconcile_interrupted_sends", "load_nodes", "load_sensors_data", "load_chats",
            "ensure_known_nodes", "normalize_unknown_nodes", "parse_nodes_from_info", "load_settings",
            "load_cpu_history", "update_base_status_from_info", "get_telemetry_from_info", "save_chats",
            "start_time_service", "start_installation_time_assignment", "start_schedule_engine",
        ):
            monkeypatch.setattr(server_module, name, lambda *a, **k: None)
        monkeypatch.setattr(server_module.telemetry, "load_telemetry", lambda *a, **k: None)
        monkeypatch.setattr(server_module.camera, "load_camera_settings", lambda *a, **k: None)
        monkeypatch.setattr(server_module, "KNOWN_NODES", {})
        monkeypatch.setattr(server_module, "camera_power_enabled_at_startup", False)
        monkeypatch.setattr(server_module, "EPAPER_ENABLED", False)
        monkeypatch.setattr(server_module.mca_runtime, "start_attachments_service", lambda *a, **k: None)
        monkeypatch.setattr(
            server_module, "restore_active_transport", lambda radio, match: restore_calls.append((radio, match))
        )
        # Only Thread is used by start_runtime() itself.
        monkeypatch.setattr(server_module, "threading", types.SimpleNamespace(Thread=_RecordingThread))

        server_module.start_runtime()
        return list(_RecordingThread.started), restore_calls

    yield _run

    if original_pause:
        server_module.pause_listen.set()
    else:
        server_module.pause_listen.clear()


@pytest.mark.parametrize("transport", ["serial", "tcp", "bluetooth"])
def test_radio_health_worker_starts_for_every_transport_when_identity_matches(run_start_runtime, transport):
    """The regression: for tcp/bluetooth this used to be False, silently
    disabling #286's status and #288's auto-reconnect."""
    started, _ = run_start_runtime(transport)

    assert "radio_health_worker" in started


def test_serial_starts_the_full_listener_worker_group(run_start_runtime):
    started, _ = run_start_runtime("serial")

    assert SERIAL_ONLY_WORKERS <= set(started)


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_non_serial_does_not_start_the_serial_listener_workers(run_start_runtime, transport):
    """Each of these consumes the `meshtastic --listen` subprocess's output
    (or only exists while it runs) - starting them for TCP/Bluetooth would
    be dead weight at best (telemetry_worker would log "No telemetry yet"
    every minute forever)."""
    started, _ = run_start_runtime(transport)

    assert not (SERIAL_ONLY_WORKERS & set(started)), sorted(SERIAL_ONLY_WORKERS & set(started))


@pytest.mark.parametrize("transport", ["serial", "tcp", "bluetooth"])
def test_no_radio_workers_start_on_identity_mismatch(run_start_runtime, transport):
    """A definitive MISMATCH never gets a health worker, on any transport."""
    started, _ = run_start_runtime(transport, identity_status="MISMATCH")

    assert "radio_health_worker" not in started
    assert not (SERIAL_ONLY_WORKERS & set(started))


@pytest.mark.parametrize("transport", ["serial", "tcp", "bluetooth"])
def test_not_found_stays_fail_closed(run_start_runtime, transport):
    started, _ = run_start_runtime(transport, identity_status="NOT_FOUND")

    assert "radio_health_worker" not in started


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_non_serial_health_worker_starts_on_transient_detection_error(run_start_runtime, transport):
    """The circular dependency: auto-reconnect exists to heal a boot-race
    DETECTION_ERROR, so that state must not keep its own worker from starting.
    Serial-only workers still must not start."""
    started, _ = run_start_runtime(transport, identity_status="DETECTION_ERROR")

    assert "radio_health_worker" in started
    assert not (SERIAL_ONLY_WORKERS & set(started))


def test_serial_health_worker_still_requires_match_on_detection_error(run_start_runtime):
    started, _ = run_start_runtime("serial", identity_status="DETECTION_ERROR")

    assert "radio_health_worker" not in started


@pytest.mark.parametrize("status,transport,expected", [
    ("MATCH", "serial", True), ("MATCH", "tcp", True),
    ("DETECTION_ERROR", "tcp", True), ("NOT_CHECKED", "bluetooth", True),
    ("DETECTION_ERROR", "serial", False), ("NOT_CHECKED", "serial", False),
    ("MISMATCH", "tcp", False), ("NOT_FOUND", "tcp", False), ("MISMATCH", "bluetooth", False),
])
def test_should_start_health_worker_table(server_module, status, transport, expected):
    assert server_module.should_start_health_worker(status, transport) is expected


@pytest.mark.parametrize("live_type,configured,expected", [
    # The pixel-111 case: router never switched off its serial default, but
    # the accepted profile is TCP.
    ("serial", "tcp", "tcp"),
    (None, "tcp", "tcp"),
    ("serial", "serial", "serial"),
    # A live non-serial transport is trusted.
    ("tcp", "tcp", "tcp"),
    ("bluetooth", "tcp", "bluetooth"),
])
def test_health_active_transport_resolution(server_module, live_type, configured, expected):
    got = server_module.resolve_health_active_transport(
        {"transport": configured, "preferred_transport": configured}, {"type": live_type}
    )

    assert got == expected


@pytest.mark.parametrize("transport", ["serial", "tcp", "bluetooth"])
def test_always_on_workers_and_transport_restore_are_unaffected(run_start_runtime, transport):
    started, restore_calls = run_start_runtime(transport)

    assert "cpu_history_worker" in started
    assert "check_worker" in started  # update_service.check_worker
    assert len(restore_calls) == 1
    assert restore_calls[0][1] is True
