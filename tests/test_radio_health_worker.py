"""Tests for compute_radio_health_status() - the transport-aware fix for
the "Radio Status" widget getting hard-stuck on STARTING for TCP/Bluetooth
(radio_health_worker() only ever updated listener_running/last_packet from
the serial `--listen` subprocess, which never runs for those transports).

radio_health_worker() itself is a `while True: time.sleep(30)` loop and is
not exercised directly here - same reasoning as
tests/test_listener_paused_recovery.py's resolve_paused_recovery_status().
"""
import pytest


@pytest.fixture
def compute(server_module):
    return server_module.compute_radio_health_status


# --- Serial: byte-for-byte regression guard -----------------------------


def test_serial_released(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=True,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={},
    )
    assert status == "RELEASED"
    assert level == "WARNING"


def test_serial_paused(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=True,
        listener_running=False,
        packet_age=None,
        transport_state={},
    )
    assert status == "PAUSED"
    assert level == "WARNING"


def test_serial_listener_down(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={},
    )
    assert status == "LISTENER_DOWN"
    assert level == "ERROR"


def test_serial_starting_no_packet_yet(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=None,
        transport_state={},
    )
    assert status == "STARTING"
    assert level == "WARNING"


def test_serial_ok_recent_packet(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=5,
        transport_state={},
    )
    assert status == "OK"
    assert level == "OK"


def test_serial_idle(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=300,
        transport_state={},
    )
    assert status == "IDLE"
    assert level == "WARNING"


def test_serial_no_packets(compute):
    status, level, reason, recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=900,
        transport_state={},
    )
    assert status == "NO_PACKETS"
    assert level == "ERROR"


def test_serial_boundary_180_is_still_ok(compute):
    status, _level, _reason, _recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=180,
        transport_state={},
    )
    assert status == "OK"


def test_serial_boundary_181_is_idle(compute):
    status, _level, _reason, _recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=181,
        transport_state={},
    )
    assert status == "IDLE"


def test_serial_ignores_transport_state(compute):
    """Serial branch must never consult transport_state - it doesn't read
    it at all, byte-for-byte the pre-fix behavior."""
    status, _level, _reason, _recommendation = compute(
        active_transport="serial",
        is_released=False,
        is_paused=False,
        listener_running=True,
        packet_age=5,
        transport_state={"state": "error", "last_error": "should be ignored"},
    )
    assert status == "OK"


# --- Non-serial (TCP/Bluetooth): no more permanent STARTING --------------


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_nonserial_connected(compute, transport):
    status, level, reason, recommendation = compute(
        active_transport=transport,
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={"state": "connected", "address": "192.168.2.34:4403"},
    )
    assert status == "CONNECTED"
    assert level == "OK"
    assert "192.168.2.34:4403" in reason


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_nonserial_connecting(compute, transport):
    status, level, reason, recommendation = compute(
        active_transport=transport,
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={"state": "connecting"},
    )
    assert status == "CONNECTING"
    assert level == "WARNING"


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_nonserial_error_uses_transport_last_error(compute, transport):
    status, level, reason, recommendation = compute(
        active_transport=transport,
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={"state": "error", "last_error": "protocol_sync_timeout: ..."},
    )
    assert status == "ERROR"
    assert level == "ERROR"
    assert reason == "protocol_sync_timeout: ..."


@pytest.mark.parametrize("transport", ["tcp", "bluetooth"])
def test_nonserial_disconnected(compute, transport):
    status, level, reason, recommendation = compute(
        active_transport=transport,
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={"state": "disconnected"},
    )
    assert status == "DISCONNECTED"
    assert level == "ERROR"


def test_nonserial_missing_state_key_defaults_to_disconnected(compute):
    """No 'state' key at all (e.g. connection_payload() returned a sparse
    dict) must fail toward DISCONNECTED, not silently stay STARTING."""
    status, level, _reason, _recommendation = compute(
        active_transport="tcp",
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={},
    )
    assert status == "DISCONNECTED"
    assert level == "ERROR"


def test_nonserial_never_stuck_on_starting(compute):
    """The actual bug this fix addresses: a connected non-serial radio
    must never report STARTING, regardless of listener_running/packet_age
    (which never populate for TCP/Bluetooth)."""
    status, _level, _reason, _recommendation = compute(
        active_transport="tcp",
        is_released=False,
        is_paused=False,
        listener_running=False,
        packet_age=None,
        transport_state={"state": "connected"},
    )
    assert status != "STARTING"
    assert status == "CONNECTED"


def test_nonserial_ignores_released_and_paused(compute):
    """RELEASED/PAUSED are serial-port-release concepts (RadioConnectionManager/
    pause_listen) - meaningless for TCP/Bluetooth, must not leak in."""
    status, _level, _reason, _recommendation = compute(
        active_transport="tcp",
        is_released=True,
        is_paused=True,
        listener_running=False,
        packet_age=None,
        transport_state={"state": "connected"},
    )
    assert status == "CONNECTED"
