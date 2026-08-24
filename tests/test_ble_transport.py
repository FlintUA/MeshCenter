"""Tests for adapters/meshtastic/ble_transport.py's connection lifecycle -
happy path, identity mismatch teardown, and NOT_CONNECTED guard on the
persistent-interface ownership model. Uses a fake BLEInterface (mocked at
the meshtastic.ble_interface module boundary) rather than real hardware -
the live smoke test on TAP2 covers the real BLEInterface/bluetoothctl
integration this can't.
"""
import sys
import threading
import time
import types

import pytest

from adapters.meshtastic.ble_transport import BLETransport
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionState,
    ConnectionType,
    OutgoingMessage,
    TransportError,
    TransportErrorCode,
)


class _FakeMyInfo:
    def __init__(self, my_node_num):
        self.my_node_num = my_node_num


class _FakePacket:
    id = 12345


class _FakeBLEInterface:
    """Stands in for meshtastic.ble_interface.BLEInterface. Records
    close() calls so identity-mismatch teardown can be asserted."""

    instances = []

    def __init__(self, address, timeout=300):
        self.address = address
        self.timeout = timeout
        self.myInfo = _FakeMyInfo(my_node_num=0x756F9960)
        self.nodes = {}
        self.localNode = types.SimpleNamespace(nodeNum=0x756F9960, channels=[])
        self.metadata = None
        self.closed = False
        _FakeBLEInterface.instances.append(self)

    def sendText(self, **kwargs):
        return _FakePacket()

    def sendData(self, *args, **kwargs):
        return _FakePacket()

    def sendWaypoint(self, **kwargs):
        return _FakePacket()

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_ble_interface_module(monkeypatch):
    _FakeBLEInterface.instances = []
    fake_module = types.ModuleType("meshtastic.ble_interface")
    fake_module.BLEInterface = _FakeBLEInterface
    monkeypatch.setitem(sys.modules, "meshtastic.ble_interface", fake_module)
    yield
    monkeypatch.delitem(sys.modules, "meshtastic.ble_interface", raising=False)


def _descriptor(address="3C:DC:75:6F:99:61"):
    return ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address=address)


def test_connect_happy_path_opens_one_persistent_interface():
    transport = BLETransport(address="3C:DC:75:6F:99:61")

    info = transport.connect(_descriptor(), timeout=5)

    assert info.state == ConnectionState.CONNECTED
    assert info.node_id == "!756f9960"
    assert transport.is_connected()
    assert len(_FakeBLEInterface.instances) == 1


def test_send_text_reuses_the_same_interface_not_a_new_one():
    transport = BLETransport(address="3C:DC:75:6F:99:61")
    transport.connect(_descriptor(), timeout=5)

    transport.send_text(OutgoingMessage(text="hi", destination_id="^all"), timeout=5)
    transport.send_text(OutgoingMessage(text="hi again", destination_id="^all"), timeout=5)

    # Ownership model: one interface opened in connect(), reused by every
    # subsequent call - never reopened per send.
    assert len(_FakeBLEInterface.instances) == 1


def test_send_text_before_connect_returns_not_connected_without_opening_interface():
    transport = BLETransport(address="3C:DC:75:6F:99:61")

    result = transport.send_text(OutgoingMessage(text="hi", destination_id="^all"), timeout=5)

    assert result.accepted is False
    assert result.error.code == TransportErrorCode.NOT_CONNECTED
    assert len(_FakeBLEInterface.instances) == 0


def test_get_nodes_before_connect_raises_not_connected():
    transport = BLETransport(address="3C:DC:75:6F:99:61")

    with pytest.raises(TransportError) as excinfo:
        transport.get_nodes(timeout=5)
    assert excinfo.value.code == TransportErrorCode.NOT_CONNECTED


def test_identity_mismatch_closes_the_interface_before_raising():
    transport = BLETransport(address="3C:DC:75:6F:99:61", expected_node_id="!deadbeef")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(), timeout=5)

    assert excinfo.value.code == TransportErrorCode.IDENTITY_MISMATCH
    # Teardown: the just-opened interface must be closed, not left dangling.
    assert len(_FakeBLEInterface.instances) == 1
    assert _FakeBLEInterface.instances[0].closed is True
    assert transport.get_connection_info().state == ConnectionState.ERROR
    assert transport.is_connected() is False


def test_reconnect_after_failed_connect_targets_last_known_good_address_not_the_failed_one(monkeypatch):
    """Regression test for the Task 47 live finding on TAP2: connect()
    used to overwrite self._address/_name unconditionally before
    attempting the new connection, with no rollback on failure - so a
    subsequent bare reconnect() (which reuses self._address/_name by
    contract, see reconnect()'s own docstring - it builds its
    ConnectionDescriptor from them, not a fresh caller-supplied address)
    would retry the address that just failed instead of the last one
    that actually worked. Also covers DEVICE_NOT_FOUND point-recognition
    for the real error text observed live: "No Meshtastic BLE peripheral
    with identifier or address '...' found. Try --ble-scan to find it."
    """
    good_address = "3C:DC:75:6F:99:61"
    bad_address = "00:11:22:33:44:55"

    class _FlakyBLEInterface(_FakeBLEInterface):
        def __init__(self, address, timeout=300):
            if address == bad_address:
                raise Exception(
                    f"No Meshtastic BLE peripheral with identifier or address '{address}' "
                    "found. Try --ble-scan to find it."
                )
            super().__init__(address, timeout=timeout)

    monkeypatch.setattr(sys.modules["meshtastic.ble_interface"], "BLEInterface", _FlakyBLEInterface)

    transport = BLETransport(address=good_address)
    transport.connect(_descriptor(good_address), timeout=5)
    assert transport.get_connection_info().state == ConnectionState.CONNECTED

    # Force-switch to a bad address - simulates POST /bluetooth/connect
    # to a mistyped/out-of-range device while already connected to a
    # working one (exactly what happened live on TAP2).
    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(bad_address), force=True, timeout=5)
    assert excinfo.value.code == TransportErrorCode.DEVICE_NOT_FOUND

    # self._address must have rolled back to the last-known-good value,
    # not stayed on the address that just failed.
    info_after_failure = transport.get_connection_info()
    assert info_after_failure.descriptor.address == good_address
    assert info_after_failure.state == ConnectionState.ERROR

    # reconnect() takes no address argument - by contract it rebuilds its
    # descriptor from self._address/_name. This must now succeed against
    # the good address, not retry the one that just failed.
    info = transport.reconnect(timeout=5)
    assert info.state == ConnectionState.CONNECTED
    assert _FakeBLEInterface.instances[-1].address == good_address


def test_disconnect_closes_interface_and_resets_state():
    transport = BLETransport(address="3C:DC:75:6F:99:61")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeBLEInterface.instances[0]

    transport.disconnect(timeout=5)

    assert interface.closed is True
    assert transport.is_connected() is False
    assert transport.get_connection_info().state == ConnectionState.DISCONNECTED


def test_disconnect_state_is_correct_even_when_close_never_returns():
    """Regression test for the ownership-transfer bug (review, live-caught
    on TAP2 - a real BLEInterface.close() took 60s+ once): before the
    fix, self._state stayed CONNECTED and self._interface stayed set
    until AFTER close() returned - so a disconnect() that timed out left
    the transport reporting itself connected to an interface a now-
    abandoned thread was still tearing down in the background. The fix
    detaches synchronously before the (possibly slow/never-returning)
    close() call even starts, so `timeout` only affects how long the
    caller waits for close() to finish - never whether is_connected()/
    get_connection_info() are correct immediately after disconnect()
    returns, timeout or not.
    """
    transport = BLETransport(address="3C:DC:75:6F:99:61")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeBLEInterface.instances[0]

    release_event = threading.Event()
    interface.close = lambda: release_event.wait(timeout=5)  # never returns in time

    transport.disconnect(timeout=0.2)  # must NOT raise - close() failures are logged, not propagated

    # State must already be correct, immediately, even though the
    # background close() call is still stuck.
    assert transport.is_connected() is False
    assert transport.get_connection_info().state == ConnectionState.DISCONNECTED

    with pytest.raises(TransportError):
        transport.get_nodes(timeout=1)  # NOT_CONNECTED, not a race on the old interface

    release_event.set()  # let the stuck background thread finish


def test_concurrent_send_and_disconnect_do_not_race_the_interface():
    """Regression test for the review finding: without self._lock, a
    concurrent disconnect() (e.g. user hits "Switch to Serial" in the UI,
    Task 46/47) could null out self._interface while a send_* call
    already in flight is still using it. Uses an exclusive-entry counter
    (same technique as SerialTransport's
    test_concurrent_connect_and_send_do_not_race_prepare_phase) - it
    would go above 1 if send_text()'s and disconnect()'s use of
    self._interface ever overlapped in wall-clock time.
    """
    transport = BLETransport(address="3C:DC:75:6F:99:61")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeBLEInterface.instances[0]

    violations = []
    in_critical_section = {"count": 0}
    counter_lock = threading.Lock()

    def _enter():
        with counter_lock:
            in_critical_section["count"] += 1
            if in_critical_section["count"] > 1:
                violations.append(in_critical_section["count"])

    def _exit():
        with counter_lock:
            in_critical_section["count"] -= 1

    def _tracked_send_text(**kwargs):
        _enter()
        time.sleep(0.15)
        _exit()
        return _FakePacket()

    def _tracked_close():
        _enter()
        time.sleep(0.1)
        interface.closed = True
        _exit()

    interface.sendText = _tracked_send_text
    interface.close = _tracked_close

    send_thread = threading.Thread(
        target=lambda: transport.send_text(OutgoingMessage(text="hi", destination_id="^all"), timeout=5)
    )
    disconnect_thread = threading.Thread(target=lambda: transport.disconnect(timeout=5))

    send_thread.start()
    time.sleep(0.02)
    disconnect_thread.start()
    send_thread.join(timeout=5)
    disconnect_thread.join(timeout=5)

    assert not send_thread.is_alive() and not disconnect_thread.is_alive()
    assert violations == [], (
        "send_text() and disconnect() overlapped their use of self._interface - "
        "self._lock is not actually serializing them"
    )


def test_reconnect_exhausts_fixed_attempts_then_raises(monkeypatch):
    """Naive reconnect (plan 5.5): fixed attempts with growing backoff,
    not infinite/exponential. Patch the delays away so the test doesn't
    actually sleep ~7s."""
    import adapters.meshtastic.ble_transport as ble_transport_module

    monkeypatch.setattr(ble_transport_module, "_RECONNECT_DELAYS_S", (0.0, 0.0))

    class _AlwaysFailBLEInterface(_FakeBLEInterface):
        def __init__(self, address, timeout=300):
            raise TransportError(TransportErrorCode.CONNECT_FAILED, "simulated failure")

    fake_module = types.ModuleType("meshtastic.ble_interface")
    fake_module.BLEInterface = _AlwaysFailBLEInterface
    monkeypatch.setitem(sys.modules, "meshtastic.ble_interface", fake_module)

    transport = BLETransport(address="3C:DC:75:6F:99:61")

    with pytest.raises(TransportError) as excinfo:
        transport.reconnect(timeout=5)

    assert excinfo.value.code == TransportErrorCode.CONNECT_FAILED
    assert transport.get_connection_info().state == ConnectionState.ERROR
