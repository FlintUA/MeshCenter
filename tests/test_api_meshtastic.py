"""Tests for api/api_meshtastic.py's register_meshtastic_routes() -
specifically the fail-closed switch()/recovery flow in _switch(), using a
REAL meshsrv.transport_router.TransportRouter (not a fake) wrapping fake
SerialTransport/BLETransport stand-ins. The router's own locking/
reassignment logic (already covered by tests/test_transport_router.py) is
not what's under test here - what's under test is whether _switch()'s
recovery path correctly repoints the router at the recovered transport,
not just reconnects it physically.

Regression coverage for the Task 47 live finding on TAP2 (second bug,
caught by the same forced-failure test that caught the _call_with_timeout
one): the first version of _switch()'s recovery path called
serial_transport.connect(...) directly instead of going through
transport_router.switch(...). That reconnected the physical serial link
fine, but transport_router.self._active stayed pointed at the still-
broken ble_transport - every subsequent send_*/get_* call kept routing to
a transport in ERROR state, live-observed as a send failing with
"BLETransport is not connected" even though the serial listener was
genuinely running with a real PID underneath.
"""
import threading
from functools import wraps

import pytest
from flask import Flask

from api.api_meshtastic import register_meshtastic_routes
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    TransportError,
    TransportErrorCode,
)
from meshsrv.transport_router import TransportRouter


class _FakeSerialTransport:
    """Connects successfully unless `fail_connect` is set - the latter
    simulates serial hardware also being unavailable, for the double-
    failure ("both down") scenario."""

    def __init__(self, fail_connect=False, listener_pid=12345):
        self.connect_calls = []
        self.fail_connect = fail_connect
        self._listener_pid = listener_pid

    def connect(self, descriptor, *, force=False, timeout=30.0):
        self.connect_calls.append(descriptor)
        if self.fail_connect:
            raise TransportError(TransportErrorCode.CONNECT_FAILED, "serial port not found")
        return self.get_connection_info()

    def disconnect(self, *, timeout=15.0):
        pass

    def get_connection_info(self):
        return ConnectionInfo(
            state=ConnectionState.CONNECTED,
            descriptor=ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyACM0"),
            node_id="!756f9960",
        )

    def get_listener_pid(self):
        return self._listener_pid

    def send_text(self, *a, **kw):
        return "sent-by-serial"


class _FakeBleTransport:
    """connect() succeeds for any address except `bad_address` - lets a
    test simulate "already connected to a good device, then a forced
    reconnect to a bad one fails" without needing real BLE hardware.
    Tracks self._state through failures the same way the real
    BLETransport.connect() does (CONNECTED -> ERROR), so a test asserting
    on get_connection_info() after a failed connect() reflects real
    behavior, not a fake artifact."""

    def __init__(self, bad_address):
        self.bad_address = bad_address
        self._state = ConnectionState.CONNECTED

    def connect(self, descriptor, *, force=False, timeout=90.0):
        if descriptor.address == self.bad_address:
            self._state = ConnectionState.ERROR
            raise TransportError(
                TransportErrorCode.DEVICE_NOT_FOUND,
                f"No Meshtastic BLE peripheral with identifier or address '{descriptor.address}' found.",
            )
        self._state = ConnectionState.CONNECTED
        return self.get_connection_info()

    def disconnect(self, *, timeout=30.0):
        pass

    def scan(self, *, timeout=15.0):
        return []

    def get_connection_info(self):
        return ConnectionInfo(
            state=self._state,
            descriptor=ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address="3C:DC:75:6F:99:61"),
            node_id="!756f9960",
        )

    def send_text(self, *a, **kw):
        raise TransportError(TransportErrorCode.NOT_CONNECTED, "BLETransport is not connected")


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500

    return wrapped


@pytest.fixture
def meshtastic_env():
    bad_address = "00:11:22:33:44:55"
    serial_transport = _FakeSerialTransport()
    ble_transport = _FakeBleTransport(bad_address=bad_address)
    # Task 48: a second, distinct fake - production passes two different
    # objects here too (the IPC-backed proxy vs. Core's listener-
    # management-only instance), see register_meshtastic_routes()'s own
    # docstring for why get_listener_pid() must never be read from the
    # same object switch operations go through. Different listener_pid
    # values (99999 vs. the other fake's default 12345) so a test can
    # prove WHICH object actually answered, not just that some number
    # came back.
    core_serial_transport = _FakeSerialTransport(listener_pid=99999)

    # Start already "on BLE", the same precondition as the live TAP2
    # repro (a prior successful switch had already made ble_transport
    # the router's active transport before the forced-failure attempt).
    transport_router = TransportRouter(ble_transport)

    settings = {"meshtastic": {"transport": "bluetooth", "ble_address": "", "ble_name": ""}}

    def save_settings():
        pass

    app = Flask(__name__)
    register_meshtastic_routes(
        app,
        _handle_errors,
        threading.Lock(),  # state_lock - not exercised concurrently, just needs to be a real context manager
        settings,
        save_settings,
        transport_router,
        serial_transport,
        ble_transport,
        "/dev/ttyACM0",
        "!756f9960",
        core_serial_transport,
    )

    return {
        "app": app,
        "client": app.test_client(),
        "transport_router": transport_router,
        "serial_transport": serial_transport,
        "ble_transport": ble_transport,
        "core_serial_transport": core_serial_transport,
        "bad_address": bad_address,
    }


def test_listener_pid_comes_from_core_serial_transport_not_the_switch_object(meshtastic_env):
    """Task 48 review requirement: listener_pid must be read from
    core_serial_transport (Core's listener-management-only instance),
    never from the `serial_transport` param that switch operations use
    (an IPC-backed proxy in production, which has no meaningful listener
    PID of its own - it lives in a different process). Distinguishable
    listener_pid values on the two fakes (99999 vs. 12345) prove which
    object actually answered, not just that a number came back."""
    client = meshtastic_env["client"]

    response = client.get("/api/meshtastic/connection")
    data = response.get_json()

    assert data["connection"]["listener_pid"] == 99999


def test_failed_switch_recovery_repoints_the_router_at_serial(meshtastic_env):
    client = meshtastic_env["client"]
    transport_router = meshtastic_env["transport_router"]
    serial_transport = meshtastic_env["serial_transport"]

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": meshtastic_env["bad_address"], "name": "NonexistentDevice"},
    )
    data = response.get_json()

    assert data["ok"] is False
    # Not "_both_down" - the serial recovery attempt itself succeeded.
    assert data["error_code"] == "transport_switch_failed"

    # THE regression check: the router's active transport must actually
    # be the recovered serial_transport, not still the broken
    # ble_transport it started on - reconnecting the physical link isn't
    # enough if the router doesn't know about it.
    assert transport_router._active is serial_transport

    # And a call through the router must actually reach serial, not
    # raise "BLETransport is not connected" the way the live bug did.
    assert transport_router.send_text("hello") == "sent-by-serial"


def test_double_failure_leaves_router_on_the_last_broken_transport_not_stuck(meshtastic_env):
    """Regression test for the reviewer's Q2: if the recovery switch()
    itself also raises (serial hardware unavailable too), does the
    exception get caught by the same `except TransportError as
    recon_err:` (it must, since transport_router.switch() propagates it
    the same way as any other call), and is self._active left in a
    coherent state - still the last real transport object (ble_transport,
    unchanged from the first failed switch), never reassigned to the
    serial_transport that also just failed to connect?
    """
    bad_address = meshtastic_env["bad_address"]
    transport_router = meshtastic_env["transport_router"]
    ble_transport = meshtastic_env["ble_transport"]
    client = meshtastic_env["client"]

    # Swap in a serial fake that also fails, after the fixture already
    # wired everything up - register_meshtastic_routes() closed over the
    # serial_transport reference, and this fake shares that identity.
    meshtastic_env["serial_transport"].fail_connect = True

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": bad_address, "name": "NonexistentDevice"},
    )
    data = response.get_json()

    assert data["ok"] is False
    assert data["error_code"] == "transport_switch_failed_both_down"

    # self._active must still be a real, valid transport object (the
    # last one that was genuinely active) - never left pointing at
    # something that also just failed, and never left in an undefined
    # state that would break the next _delegate() call.
    assert transport_router._active is ble_transport
    info = transport_router.get_connection_info()
    assert info.state == ConnectionState.ERROR  # ble_transport's own connect() failure, tracked honestly


def test_successful_switch_still_works_normally(meshtastic_env):
    client = meshtastic_env["client"]
    transport_router = meshtastic_env["transport_router"]
    ble_transport = meshtastic_env["ble_transport"]

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": "3C:DC:75:6F:99:61", "name": "FLT2_9960"},
    )
    data = response.get_json()

    assert data["ok"] is True
    assert transport_router._active is ble_transport
