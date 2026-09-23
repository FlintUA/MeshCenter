"""Tests for api/api_meshtastic.py's register_meshtastic_routes() -
specifically the fail-closed switch()/recovery flow in _switch(), using a
REAL meshsrv.transport_router.TransportRouter (not a fake) wrapping fake
SerialTransport/BLETransport/TCPTransport stand-ins. The router's own
locking/reassignment logic (already covered by tests/test_transport_router.py)
is not what's under test here - what's under test is whether _switch()'s
recovery path correctly repoints the router at the recovered transport, not
just reconnects it physically, and (Radio TCP Transport part 2) that
recovery targets whatever was ACTUALLY the previously-active transport
(read from settings.meshtastic.transport), not a hardcoded serial fallback.

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
    NodeInfo,
    NodeUser,
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
        self._state = ConnectionState.CONNECTED

    def connect(self, descriptor, *, force=False, timeout=30.0):
        self.connect_calls.append(descriptor)
        if self.fail_connect:
            self._state = ConnectionState.ERROR
            raise TransportError(TransportErrorCode.CONNECT_FAILED, "serial port not found")
        self._state = ConnectionState.CONNECTED
        return self.get_connection_info()

    def disconnect(self, *, timeout=15.0):
        pass

    def get_connection_info(self):
        return ConnectionInfo(
            state=self._state,
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
        self.connect_calls = []

    def connect(self, descriptor, *, force=False, timeout=90.0):
        self.connect_calls.append(descriptor)
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


class _FakeTcpTransport:
    """connect() succeeds for any (host, port) except `bad_host` - mirrors
    _FakeBleTransport's shape for the TCP counterpart.

    `identity_node_id`/`identity_long_name` (Radio TCP Transport part 2
    correction pass #4) are what get_local_node() reports post-connect -
    defaults match get_connection_info()'s own node_id so a fresh env is
    self-consistent unless a test deliberately wants a mismatch.
    `fail_identity_read` simulates the connect succeeding but the
    identity-check round-trip itself failing (radio disappeared right
    after connect)."""

    def __init__(self, bad_host=None, identity_node_id="!1fa065f0", identity_long_name="T-Beam", fail_identity_read=False):
        self.bad_host = bad_host
        self._state = ConnectionState.CONNECTED
        self.connect_calls = []
        self.get_local_node_calls = 0
        self.identity_node_id = identity_node_id
        self.identity_long_name = identity_long_name
        self.fail_identity_read = fail_identity_read

    def connect(self, descriptor, *, force=False, timeout=30.0):
        self.connect_calls.append(descriptor)
        host = descriptor.address.split(":", 1)[0]
        if self.bad_host is not None and host == self.bad_host:
            self._state = ConnectionState.ERROR
            raise TransportError(TransportErrorCode.CONNECT_REFUSED, f"{descriptor.address} refused the connection")
        self._state = ConnectionState.CONNECTED
        return self.get_connection_info()

    def disconnect(self, *, timeout=15.0):
        pass

    def get_connection_info(self):
        return ConnectionInfo(
            state=self._state,
            descriptor=ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403"),
            node_id=self.identity_node_id,
        )

    def get_local_node(self, *, timeout=15.0):
        self.get_local_node_calls += 1
        if self.fail_identity_read:
            raise TransportError(TransportErrorCode.TIMEOUT, "identity read timed out")
        return NodeInfo(
            node_id=self.identity_node_id,
            num=0,
            user=NodeUser(
                id=self.identity_node_id, long_name=self.identity_long_name, short_name="TST", hw_model="TBEAM"
            ),
        )

    def get_metadata(self, *, timeout=15.0):
        return {"metadata_json": "{}"}

    def send_text(self, *a, **kw):
        return "sent-by-tcp"


class _FakeInstanceManager:
    """Records .save() calls so a test can assert _persist_choice() wrote
    through INSTANCE_IDENTITY.radio.transport/endpoint, not just
    settings.meshtastic - the whole point of that write-through (see
    server.py's start_runtime() TRANSPORT RESTORE block, which reads it)."""

    def __init__(self, initial=None):
        self._identity = dict(initial or {"radio": {}})
        self.saved = []

    def get(self):
        return dict(self._identity)

    def save(self, updated):
        self._identity = dict(updated)
        self.saved.append(dict(updated))
        return dict(self._identity)


def _handle_errors(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as error:
            return {"ok": False, "error": str(error)}, 500

    return wrapped


def _register(
    *,
    transport_router,
    serial_transport,
    ble_transport,
    tcp_transport,
    settings,
    serial_port="/dev/ttyACM0",
    instance_manager=None,
):
    core_serial_transport = _FakeSerialTransport(listener_pid=99999)

    def save_settings():
        pass

    instance_manager = instance_manager or _FakeInstanceManager()

    app = Flask(__name__)
    register_meshtastic_routes(
        app,
        _handle_errors,
        threading.Lock(),
        settings,
        save_settings,
        transport_router,
        serial_transport,
        ble_transport,
        tcp_transport,
        serial_port,
        "!756f9960",
        core_serial_transport,
        instance_manager,
    )
    return {
        "app": app,
        "client": app.test_client(),
        "core_serial_transport": core_serial_transport,
        "instance_manager": instance_manager,
    }


@pytest.fixture
def meshtastic_env():
    """Baseline: already connected on Bluetooth, settings.meshtastic
    persists "bluetooth" with NO saved address (the address was cleared/
    never actually persisted) - this is what makes the "no viable
    previous transport" tests below realistic, not contrived: the
    persisted choice and the live router state can legitimately disagree."""
    bad_address = "00:11:22:33:44:55"
    serial_transport = _FakeSerialTransport()
    ble_transport = _FakeBleTransport(bad_address=bad_address)
    tcp_transport = _FakeTcpTransport()
    transport_router = TransportRouter(ble_transport)
    settings = {"meshtastic": {"transport": "bluetooth", "ble_address": "", "ble_name": ""}}

    env = _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
    )
    env.update({
        "transport_router": transport_router,
        "serial_transport": serial_transport,
        "ble_transport": ble_transport,
        "tcp_transport": tcp_transport,
        "settings": settings,
        "bad_address": bad_address,
    })
    return env


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


def test_failed_switch_with_no_viable_previous_transport_reports_failure_without_reconnecting_anything(meshtastic_env):
    """Radio TCP Transport part 2's own explicit requirement: a failed
    switch on a host with no viable previous transport to recover to
    (here: settings says "bluetooth" was previously active, but its own
    saved ble_address is empty - nothing to actually recover with) must
    NOT reconnect anything else - report the failure and leave
    transport_router exactly where the failed connect_new() left it."""
    client = meshtastic_env["client"]
    transport_router = meshtastic_env["transport_router"]
    ble_transport = meshtastic_env["ble_transport"]
    serial_transport = meshtastic_env["serial_transport"]

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": meshtastic_env["bad_address"], "name": "NonexistentDevice"},
    )
    data = response.get_json()

    assert data["ok"] is False
    # No recovery was even attempted (nothing viable to recover to) - the
    # plain single-failure code, not "_both_down" (that implies a
    # recovery attempt happened and ALSO failed).
    assert data["error_code"] == "transport_switch_failed"

    # transport_router is left exactly where the failed connect_new()
    # left it - still ble_transport (never reassigned on a raise), never
    # silently rebound to serial_transport or anything else.
    assert transport_router._active is ble_transport
    assert serial_transport.connect_calls == []  # never touched


def test_failed_switch_recovers_to_the_actual_previous_transport(meshtastic_env):
    """When the previously-active transport (per settings.meshtastic,
    the persisted last-known-good choice) IS viable - a real saved
    address/port exists for it - a failed switch recovers to THAT
    transport, through the router, exactly as the pre-existing
    (previously serial-only) recovery path already did. Here: previously
    serial, attempted switch to a bad Bluetooth address fails, recovers
    back to serial."""
    serial_transport = _FakeSerialTransport()
    bad_address = "00:11:22:33:44:55"
    ble_transport = _FakeBleTransport(bad_address=bad_address)
    tcp_transport = _FakeTcpTransport()
    transport_router = TransportRouter(serial_transport)
    settings = {"meshtastic": {"transport": "serial"}}

    env = _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
    )
    client = env["client"]

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": bad_address, "name": "NonexistentDevice"},
    )
    data = response.get_json()

    assert data["ok"] is False
    assert data["error_code"] == "transport_switch_failed"

    # THE regression check: the router's active transport must actually
    # be the recovered serial_transport, and a call through the router
    # must actually reach it, not raise "not connected" the way the live
    # bug did.
    assert transport_router._active is serial_transport
    assert transport_router.send_text("hello") == "sent-by-serial"


def test_failed_switch_does_not_recover_to_the_transport_that_just_failed(meshtastic_env):
    """If the previously-active transport (per settings) is the SAME
    type as the one that just failed (switching to a different device on
    the same transport type), recovering "to itself" makes no sense -
    _previous_transport_recovery()'s own `exclude` guard must skip it,
    not attempt a second connect() against the very target that just
    failed."""
    bad_address = "00:11:22:33:44:55"
    serial_transport = _FakeSerialTransport()
    ble_transport = _FakeBleTransport(bad_address=bad_address)
    tcp_transport = _FakeTcpTransport()
    transport_router = TransportRouter(ble_transport)
    # Previously bluetooth, WITH a viable saved address - but switching
    # target is also bluetooth (a different, bad address).
    settings = {"meshtastic": {"transport": "bluetooth", "ble_address": "3C:DC:75:6F:99:61", "ble_name": "Good"}}

    env = _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
    )
    client = env["client"]

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": bad_address, "name": "NonexistentDevice"},
    )
    data = response.get_json()

    assert data["ok"] is False
    assert data["error_code"] == "transport_switch_failed"
    # No second bluetooth connect() attempt beyond the one that already
    # failed - recovery was correctly skipped (excluded), not retried.
    assert len(ble_transport.connect_calls) == 1
    assert transport_router._active is ble_transport


def test_double_failure_leaves_router_on_the_last_broken_transport_not_stuck():
    """Regression test for the reviewer's Q2: if the recovery switch()
    itself also raises (serial hardware unavailable too), does the
    exception get caught by the same `except TransportError as
    recon_err:` (it must, since transport_router.switch() propagates it
    the same way as any other call), and is self._active left in a
    coherent state - still the last real transport object (ble_transport,
    unchanged from the first failed switch), never reassigned to the
    serial_transport that also just failed to connect?
    """
    bad_address = "00:11:22:33:44:55"
    serial_transport = _FakeSerialTransport(fail_connect=True)
    ble_transport = _FakeBleTransport(bad_address=bad_address)
    tcp_transport = _FakeTcpTransport()
    transport_router = TransportRouter(ble_transport)
    settings = {"meshtastic": {"transport": "serial"}}  # a viable-looking previous transport that will ALSO fail

    env = _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
    )
    client = env["client"]

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


def test_successful_switch_writes_through_to_instance_identity(meshtastic_env):
    """_persist_choice() must update INSTANCE_IDENTITY.radio.transport/
    endpoint on every successful switch, not just settings.meshtastic -
    this is the boot-time source of truth server.py's start_runtime()
    TRANSPORT RESTORE block reads (see that block's own comment)."""
    client = meshtastic_env["client"]
    instance_manager = meshtastic_env["instance_manager"]

    response = client.post(
        "/api/meshtastic/bluetooth/connect",
        json={"address": "3C:DC:75:6F:99:61", "name": "FLT2_9960"},
    )
    assert response.get_json()["ok"] is True

    assert instance_manager.saved, "instance_manager.save() was never called"
    saved_radio = instance_manager.saved[-1]["radio"]
    assert saved_radio["transport"] == "bluetooth"
    assert saved_radio["endpoint"] == {"address": "3C:DC:75:6F:99:61", "label": "FLT2_9960"}


# ---------------------------------------------------------------------------
# TCP connect route
# ---------------------------------------------------------------------------

@pytest.fixture
def tcp_env():
    serial_transport = _FakeSerialTransport()
    ble_transport = _FakeBleTransport(bad_address="00:00:00:00:00:00")
    tcp_transport = _FakeTcpTransport(bad_host="10.0.0.99")
    transport_router = TransportRouter(serial_transport)
    settings = {"meshtastic": {"transport": "serial"}}

    env = _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
    )
    env.update({
        "transport_router": transport_router,
        "serial_transport": serial_transport,
        "tcp_transport": tcp_transport,
        "settings": settings,
    })
    return env


def test_tcp_connect_requires_host(tcp_env):
    response = tcp_env["client"].post("/api/meshtastic/tcp/connect", json={"port": 4403})
    data = response.get_json()
    assert data["ok"] is False
    assert data["error_code"] == "tcp_host_required"


def test_tcp_connect_rejects_non_integer_port(tcp_env):
    response = tcp_env["client"].post(
        "/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": "not-a-number"}
    )
    data = response.get_json()
    assert data["ok"] is False
    assert data["error_code"] == "tcp_port_invalid"


@pytest.mark.parametrize("bad_port", [0, -1, 65536, 100000])
def test_tcp_connect_rejects_out_of_range_port(tcp_env, bad_port):
    response = tcp_env["client"].post(
        "/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": bad_port}
    )
    data = response.get_json()
    assert data["ok"] is False
    assert data["error_code"] == "tcp_port_invalid"


def test_tcp_connect_defaults_port_when_omitted(tcp_env):
    response = tcp_env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34"})
    data = response.get_json()
    assert data["ok"] is True
    assert data["connection"]["address"] == "192.168.2.34:4403"


def test_tcp_connect_success_switches_the_router(tcp_env):
    client = tcp_env["client"]
    transport_router = tcp_env["transport_router"]
    tcp_transport = tcp_env["tcp_transport"]

    response = client.post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    data = response.get_json()

    assert data["ok"] is True
    assert transport_router._active is tcp_transport
    assert data["connection"]["type"] == "tcp"
    assert data["connection"]["node_id"] == "!1fa065f0"


def test_tcp_connect_failure_recovers_to_previous_serial(tcp_env):
    client = tcp_env["client"]
    transport_router = tcp_env["transport_router"]
    serial_transport = tcp_env["serial_transport"]

    response = client.post("/api/meshtastic/tcp/connect", json={"host": "10.0.0.99", "port": 4403})
    data = response.get_json()

    assert data["ok"] is False
    assert data["error_code"] == "transport_switch_failed"
    assert transport_router._active is serial_transport


def test_set_transport_tcp_reconnects_last_used_endpoint(tcp_env):
    client = tcp_env["client"]
    transport_router = tcp_env["transport_router"]
    tcp_transport = tcp_env["tcp_transport"]

    # First connect to establish a "last used" TCP endpoint in settings.
    connect_response = client.post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    assert connect_response.get_json()["ok"] is True

    # Switch away to serial, then back via the generic switch route -
    # must reconnect to 192.168.2.34:4403 without the caller repeating it.
    client.post("/api/meshtastic/transport", json={"type": "serial"})
    response = client.post("/api/meshtastic/transport", json={"type": "tcp"})
    data = response.get_json()

    assert data["ok"] is True
    assert transport_router._active is tcp_transport
    assert tcp_transport.connect_calls[-1].address == "192.168.2.34:4403"


def test_set_transport_tcp_without_a_previous_connect_is_a_clean_400(tcp_env):
    response = tcp_env["client"].post("/api/meshtastic/transport", json={"type": "tcp"})
    data = response.get_json()
    assert data["ok"] is False
    assert data["error_code"] == "tcp_host_required"


def test_set_transport_rejects_unknown_type(tcp_env):
    response = tcp_env["client"].post("/api/meshtastic/transport", json={"type": "carrier_pigeon"})
    data = response.get_json()
    assert data["ok"] is False
    assert data["error_code"] == "invalid_transport_type"


# ---------------------------------------------------------------------------
# TCP post-connect identity check (Radio TCP Transport part 2, correction
# pass #4) - "Settings -> TCP Connect" reconnects the SAME accepted profile
# over TCP, it is not an onboarding flow. Regression items #4 (restart
# reaches MATCH and restores TCP) and #5 (stale serial data never causes
# serial probing when transport=tcp) are covered by the pre-existing
# tests/test_server_startup_tcp_transport.py suite (verify_radio_identity()'s
# transport branching, restore_active_transport()) - untouched by this
# correction, not re-tested here.
# ---------------------------------------------------------------------------

def _accepted_env(accepted_radio, **tcp_kwargs):
    serial_transport = _FakeSerialTransport()
    ble_transport = _FakeBleTransport(bad_address="00:00:00:00:00:00")
    tcp_transport = _FakeTcpTransport(**tcp_kwargs)
    transport_router = TransportRouter(serial_transport)
    settings = {"meshtastic": {"transport": "serial"}}
    instance_manager = _FakeInstanceManager(initial={"radio": accepted_radio})

    env = _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
        instance_manager=instance_manager,
    )
    env.update({
        "transport_router": transport_router,
        "serial_transport": serial_transport,
        "tcp_transport": tcp_transport,
        "settings": settings,
        "instance_manager": instance_manager,
    })
    return env


def test_tcp_connect_to_the_same_accepted_node_persists_a_coherent_record():
    """Regression #1: a successful TCP connect to the already-accepted
    node persists identity + transport + endpoint TOGETHER, not just
    transport/endpoint with a stale identity left over (the pixel-111
    live finding this correction fixes)."""
    env = _accepted_env(
        {"node_id": "!1fa065f0", "long_name": "Old Name", "port": "/dev/ttyACM0"},
        identity_node_id="!1fa065f0", identity_long_name="T-Beam",
    )
    response = env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    data = response.get_json()

    assert data["ok"] is True
    saved_radio = env["instance_manager"].get()["radio"]
    assert saved_radio["node_id"] == "!1fa065f0"
    assert saved_radio["long_name"] == "T-Beam"  # refreshed from the live TCP read, not the stale accepted value
    assert saved_radio["transport"] == "tcp"
    assert saved_radio["endpoint"] == {"host": "192.168.2.34", "port": 4403}
    # Regression #2 (this record's shape is what a restart reads back):
    # legacy serial "port" must not carry stale meaning for a TCP record.
    assert saved_radio["port"] == ""


def test_tcp_connect_preserves_a_previously_remembered_serial_connection():
    """Multi-connection model (Radio Profiles & Connections Model, PR 1):
    switching to TCP for a radio that was previously connected over
    serial must not drop the earlier serial connections entry -
    _persist_choice() now merges via remember_connection() instead of
    plainly overwriting radio["endpoint"], which used to silently forget
    every other transport on each switch."""
    accepted_radio = {
        "node_id": "!1fa065f0", "long_name": "T-Beam", "port": "/dev/ttyACM0",
        "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
        "connections": {"serial": {"endpoint": {"port": "/dev/ttyACM0"}}},
        "preferred_transport": "serial", "last_successful_transport": "serial",
    }
    env = _accepted_env(
        accepted_radio,
        identity_node_id="!1fa065f0", identity_long_name="T-Beam",
    )

    response = env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    assert response.get_json()["ok"] is True

    saved_radio = env["instance_manager"].get()["radio"]
    assert saved_radio["connections"]["serial"] == {"endpoint": {"port": "/dev/ttyACM0"}}
    assert saved_radio["connections"]["tcp"]["endpoint"] == {"host": "192.168.2.34", "port": 4403}
    assert saved_radio["preferred_transport"] == "tcp"
    assert saved_radio["last_successful_transport"] == "tcp"


def test_tcp_connect_to_a_different_node_is_rejected_not_silently_persisted():
    """Regression #3: connecting to a DIFFERENT node than the accepted
    one over TCP must not silently mutate the current profile - explicit
    refusal, nothing persisted, and the router is reverted (not left
    "live" on the mismatched radio)."""
    env = _accepted_env(
        {"node_id": "!756f9960", "long_name": "Flint TAP2", "port": "/dev/ttyACM0"},
        identity_node_id="!1fa065f0", identity_long_name="T-Beam",
    )
    transport_router = env["transport_router"]
    serial_transport = env["serial_transport"]

    response = env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    data = response.get_json()

    assert data["ok"] is False
    assert data["error_code"] == "identity_mismatch"
    assert env["instance_manager"].saved == []  # _persist_choice() never ran
    # Reverted to the previous transport (serial) via the router, not left
    # pointed at the just-connected-but-rejected TCP transport.
    assert transport_router._active is serial_transport


def test_tcp_identity_read_failure_after_connect_is_also_rejected_and_reverted():
    """A TCP connect that succeeds at the socket/protocol level but whose
    post-connect identity read itself fails must be treated the same as
    a mismatch - not silently persisted as if it were verified."""
    env = _accepted_env(
        {"node_id": "!1fa065f0", "long_name": "T-Beam", "port": "/dev/ttyACM0"},
        fail_identity_read=True,
    )
    transport_router = env["transport_router"]
    serial_transport = env["serial_transport"]

    response = env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    data = response.get_json()

    assert data["ok"] is False
    assert data["error_code"] == "tcp_identity_check_failed"
    assert env["instance_manager"].saved == []
    assert transport_router._active is serial_transport


def test_tcp_acceptance_does_not_touch_active_profile_id():
    """Regression #6: instance.json's active_profile_id (and by extension
    whatever profile metadata it points at) stays exactly as it was -
    _persist_choice() only ever touches the "radio" sub-record, never
    active_profile_id, for either the MATCH or fresh-onboarding case."""
    env = _accepted_env(
        {"node_id": "!1fa065f0", "long_name": "T-Beam", "port": "/dev/ttyACM0"},
        identity_node_id="!1fa065f0", identity_long_name="T-Beam",
    )
    env["instance_manager"].save({
        **env["instance_manager"].get(),
        "active_profile_id": "1fa065f0",
    })

    response = env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})

    assert response.get_json()["ok"] is True
    assert env["instance_manager"].get()["active_profile_id"] == "1fa065f0"


def test_fresh_install_tcp_connect_establishes_identity_as_onboarding():
    """Regression #7: no accepted identity yet (node_id empty) -> TCP
    connect establishes it as first onboarding, no mismatch rejection."""
    env = _accepted_env({}, identity_node_id="!1fa065f0", identity_long_name="T-Beam")

    response = env["client"].post("/api/meshtastic/tcp/connect", json={"host": "192.168.2.34", "port": 4403})
    data = response.get_json()

    assert data["ok"] is True
    saved_radio = env["instance_manager"].get()["radio"]
    assert saved_radio["node_id"] == "!1fa065f0"
    assert saved_radio["long_name"] == "T-Beam"
    assert saved_radio["transport"] == "tcp"


# ---------------------------------------------------------------------------
# POST /api/meshtastic/connections/<transport>/forget (Radio Profiles &
# Connections Model, PR 3b)
# ---------------------------------------------------------------------------

def _forget_env(radio):
    serial_transport = _FakeSerialTransport()
    ble_transport = _FakeBleTransport(bad_address="00:00:00:00:00:00")
    tcp_transport = _FakeTcpTransport()
    transport_router = TransportRouter(serial_transport)
    settings = {"meshtastic": {"transport": radio.get("transport", "serial")}}
    instance_manager = _FakeInstanceManager(initial={"radio": radio})

    return _register(
        transport_router=transport_router,
        serial_transport=serial_transport,
        ble_transport=ble_transport,
        tcp_transport=tcp_transport,
        settings=settings,
        instance_manager=instance_manager,
    )


def test_forget_connection_removes_a_non_preferred_saved_connection():
    env = _forget_env({
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}},
            "serial": {"endpoint": {"port": "/dev/ttyACM0"}},
        },
        "preferred_transport": "tcp",
    })

    response = env["client"].post("/api/meshtastic/connections/serial/forget")
    data = response.get_json()

    assert response.status_code == 200
    assert data["ok"] is True
    assert "serial" not in data["connections"]
    assert "tcp" in data["connections"]
    saved_radio = env["instance_manager"].get()["radio"]
    assert "serial" not in saved_radio["connections"]


def test_forget_connection_refuses_to_remove_the_preferred_transport():
    env = _forget_env({
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}},
            "serial": {"endpoint": {"port": "/dev/ttyACM0"}},
        },
        "preferred_transport": "tcp",
    })

    response = env["client"].post("/api/meshtastic/connections/tcp/forget")
    data = response.get_json()

    assert response.status_code == 409
    assert data["ok"] is False
    assert data["error_code"] == "cannot_remove_preferred"
    # Nothing persisted - both connections still there.
    saved_radio = env["instance_manager"].get()["radio"]
    assert set(saved_radio["connections"].keys()) == {"tcp", "serial"}


def test_forget_connection_404s_for_a_transport_with_nothing_saved():
    env = _forget_env({
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}},
        "preferred_transport": "tcp",
    })

    response = env["client"].post("/api/meshtastic/connections/bluetooth/forget")
    data = response.get_json()

    assert response.status_code == 404
    assert data["ok"] is False
    assert data["error_code"] == "connection_not_found"


def test_forget_connection_rejects_an_unknown_transport_name():
    env = _forget_env({"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}})

    response = env["client"].post("/api/meshtastic/connections/carrier_pigeon/forget")
    data = response.get_json()

    assert response.status_code == 400
    assert data["ok"] is False
    assert data["error_code"] == "invalid_transport_type"
