"""Auto/manual reconnect after an adapter respawn, and dead-session detection.

Live incident (pixel-111, 2026-09-26): every `tcp_connected`/timeout error
recycles the adapter subprocess by design, and the fresh process builds
TCPTransport(host="") - so the following bare reconnect() failed all six
backoff attempts with `dns_error: could not resolve ''` (~48s of sleeps under
the router lock, which also made a manual switch() BUSY). Separately a session
whose reader thread had died (Connection reset by peer) stayed READY -
reported CONNECTED - until something happened to send, so the health worker
never saw an error to recover from. Core's get_connection_info() is cache-only,
so the adapter's own view must also be pulled across IPC.
"""
import threading
import time
import types

import pytest

from adapters.meshtastic import tcp_transport as tcp_mod
from adapters.meshtastic.ipc_server import _AdapterDispatcher
from adapters.meshtastic.tcp_transport import TCPTransport
from meshsrv import ipc_protocol
from meshsrv.adapter_ipc_client import AdapterIPCTransport
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    TransportError,
    TransportErrorCode,
)
from meshsrv.transport_router import TransportRouter


def _tcp(address="192.168.2.34:4403", label=""):
    return ConnectionDescriptor(type=ConnectionType.TCP, address=address, label=label)


# ---------------------------------------------------------------------------
# TCPTransport: fail fast without an endpoint, adopt an explicit one
# ---------------------------------------------------------------------------


def test_reconnect_without_any_endpoint_fails_immediately(monkeypatch):
    slept = []
    monkeypatch.setattr(tcp_mod.time, "sleep", lambda s: slept.append(s))
    opened = []
    monkeypatch.setattr(tcp_mod.socket, "create_connection", lambda *a, **k: opened.append(a))

    transport = TCPTransport(host="")  # exactly what ipc_server builds in a fresh process
    with pytest.raises(TransportError) as excinfo:
        transport.reconnect(timeout=150.0)

    assert excinfo.value.code == TransportErrorCode.CONNECT_FAILED
    assert "explicit address" in excinfo.value.message
    assert slept == [], "no backoff sleeps (they ran ~48s under the router lock)"
    assert opened == [], "no socket was even attempted against ''"


def test_adopt_endpoint_sets_host_port_label():
    transport = TCPTransport(host="")
    transport.adopt_endpoint(_tcp("192.168.2.34:4403", label="T-Beam"))

    info = transport.get_connection_info()
    assert info.descriptor.address == "192.168.2.34:4403"
    assert info.descriptor.label == "T-Beam"


@pytest.mark.parametrize("descriptor", [
    None,
    ConnectionDescriptor(type=ConnectionType.BLUETOOTH, address="AA:BB"),
    ConnectionDescriptor(type=ConnectionType.TCP, address=""),
])
def test_adopt_endpoint_ignores_unusable_descriptors(descriptor):
    transport = TCPTransport(host="10.0.0.5", port=4403)
    transport.adopt_endpoint(descriptor)

    assert transport.get_connection_info().descriptor.address == "10.0.0.5:4403"


def test_reconnect_after_adopting_an_endpoint_targets_that_address(monkeypatch):
    monkeypatch.setattr(tcp_mod.time, "sleep", lambda s: None)
    attempts = []

    def fake_connect(self, descriptor, *, force=False, timeout=30.0):
        attempts.append((descriptor.address, force))
        return self.get_connection_info()

    monkeypatch.setattr(TCPTransport, "connect", fake_connect)
    transport = TCPTransport(host="")
    transport.adopt_endpoint(_tcp("192.168.2.34:4403"))
    transport.reconnect(timeout=30.0)

    assert attempts == [("192.168.2.34:4403", True)]


# ---------------------------------------------------------------------------
# TCPTransport: a READY session with a dead reader is not CONNECTED
# ---------------------------------------------------------------------------


class _Reader:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


def _ready_transport(interface):
    transport = TCPTransport(host="192.168.2.34", port=4403)
    transport._interface = interface
    transport._state = tcp_mod._TcpState.READY
    transport._connected_since = 1000.0
    return transport


def test_dead_reader_thread_demotes_ready_to_error():
    interface = types.SimpleNamespace(_rxThread=_Reader(alive=True))
    transport = _ready_transport(interface)
    assert transport.get_connection_info().state == ConnectionState.CONNECTED
    assert transport.is_connected() is True

    interface._rxThread.alive = False  # "Unexpected OSError, terminating meshtastic reader... reset by peer"

    assert transport.is_connected() is False
    info = transport.get_connection_info()
    assert info.state == ConnectionState.ERROR
    assert info.last_error.code == TransportErrorCode.REMOTE_DISCONNECT
    assert info.connected_since is None


def test_cleared_isconnected_event_also_demotes():
    connected = threading.Event()
    connected.set()
    interface = types.SimpleNamespace(_rxThread=_Reader(True), isConnected=connected)
    transport = _ready_transport(interface)
    assert transport.get_connection_info().state == ConnectionState.CONNECTED

    connected.clear()

    assert transport.get_connection_info().state == ConnectionState.ERROR


def test_healthy_session_and_unknown_library_shape_stay_connected():
    healthy = _ready_transport(types.SimpleNamespace(_rxThread=_Reader(True)))
    assert healthy.get_connection_info().state == ConnectionState.CONNECTED

    # A library version without these attributes: "no evidence of a problem".
    opaque = _ready_transport(types.SimpleNamespace())
    assert opaque.get_connection_info().state == ConnectionState.CONNECTED


def test_demotion_keeps_the_interface_so_the_next_connect_closes_it():
    interface = types.SimpleNamespace(_rxThread=_Reader(False))
    transport = _ready_transport(interface)
    transport.get_connection_info()

    assert transport._interface is interface


# ---------------------------------------------------------------------------
# ipc_server: reconnect carries the endpoint; connection_info operation
# ---------------------------------------------------------------------------


class _Target:
    def __init__(self):
        self.calls = []

    def adopt_endpoint(self, descriptor):
        self.calls.append(("adopt", descriptor.address))

    def reconnect(self, *, timeout):
        self.calls.append(("reconnect", timeout))
        return ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=_tcp(), node_id="!1fa065f0")

    def get_connection_info(self):
        return ConnectionInfo(state=ConnectionState.ERROR, descriptor=_tcp(), node_id=None)


def _dispatcher_for(target):
    dispatcher = _AdapterDispatcher.__new__(_AdapterDispatcher)
    dispatcher._target = lambda transport_type: target
    return dispatcher


def _request(operation, params=None, transport_type="tcp"):
    return {
        "protocol_version": ipc_protocol.PROTOCOL_VERSION,
        "operation": operation,
        "transport_type": transport_type,
        "params": params or {},
        "timeout": 30.0,
    }


def test_ipc_reconnect_adopts_the_supplied_endpoint_before_reconnecting():
    target = _Target()
    response = _dispatcher_for(target).handle(_request(
        "reconnect", {"descriptor": ipc_protocol.descriptor_to_dict(_tcp("192.168.2.34:4403"))}
    ))

    assert response["ok"] is True
    assert [c[0] for c in target.calls] == ["adopt", "reconnect"]
    assert target.calls[0] == ("adopt", "192.168.2.34:4403")


def test_ipc_reconnect_without_descriptor_is_unchanged():
    target = _Target()
    _dispatcher_for(target).handle(_request("reconnect"))

    assert [c[0] for c in target.calls] == ["reconnect"]


def test_ipc_connection_info_reports_the_adapters_own_view():
    response = _dispatcher_for(_Target()).handle(_request("connection_info"))

    assert response["ok"] is True
    assert ipc_protocol.connection_info_from_dict(response["result"]).state == ConnectionState.ERROR


# ---------------------------------------------------------------------------
# AdapterIPCTransport: descriptor on reconnect, refresh_connection_info
# ---------------------------------------------------------------------------


class _Supervisor:
    def __init__(self, result=None, error=None):
        self.requests = []
        self.result = result
        self.error = error

    def call(self, request, *, timeout, ble_address_for_cleanup):
        self.requests.append(request)
        if self.error:
            raise self.error
        return {"ok": True, "result": self.result}


def _info_dict(state, address="192.168.2.34:4403", node_id="!1fa065f0"):
    return ipc_protocol.connection_info_to_dict(
        ConnectionInfo(state=state, descriptor=_tcp(address), node_id=node_id)
    )


def test_client_reconnect_sends_the_explicit_descriptor():
    supervisor = _Supervisor(result=_info_dict(ConnectionState.CONNECTED))
    client = AdapterIPCTransport(ConnectionType.TCP, supervisor)

    client.reconnect(timeout=30.0, descriptor=_tcp("192.168.2.34:4403"))

    assert supervisor.requests[0]["operation"] == "reconnect"
    assert supervisor.requests[0]["params"]["descriptor"]["address"] == "192.168.2.34:4403"


def test_client_reconnect_without_descriptor_sends_no_params():
    supervisor = _Supervisor(result=_info_dict(ConnectionState.CONNECTED))
    AdapterIPCTransport(ConnectionType.TCP, supervisor).reconnect(timeout=30.0)

    assert supervisor.requests[0]["params"] == {}


def test_refresh_pulls_a_dead_session_into_the_core_cache():
    supervisor = _Supervisor(result=_info_dict(ConnectionState.ERROR))
    client = AdapterIPCTransport(ConnectionType.TCP, supervisor)
    client._cached_info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=_tcp(), node_id="!1fa065f0")
    assert client.get_connection_info().state == ConnectionState.CONNECTED  # stale cache

    client.refresh_connection_info()

    assert supervisor.requests[0]["operation"] == "connection_info"
    assert client.get_connection_info().state == ConnectionState.ERROR


def test_refresh_keeps_the_cached_endpoint_when_a_respawned_adapter_reports_none():
    """A fresh adapter process knows no host (":4403"); Core's cached
    descriptor is the real address and must survive."""
    supervisor = _Supervisor(result=_info_dict(ConnectionState.DISCONNECTED, address=":4403", node_id=None))
    client = AdapterIPCTransport(ConnectionType.TCP, supervisor)
    client._cached_info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=_tcp(), node_id="!1fa065f0")

    info = client.refresh_connection_info()

    assert info.state == ConnectionState.DISCONNECTED
    assert info.descriptor.address == "192.168.2.34:4403"
    assert info.node_id == "!1fa065f0"


def test_refresh_failure_marks_the_cache_error_and_raises():
    supervisor = _Supervisor(error=TransportError(TransportErrorCode.ADAPTER_UNAVAILABLE, "gone"))
    client = AdapterIPCTransport(ConnectionType.TCP, supervisor)

    with pytest.raises(TransportError):
        client.refresh_connection_info()
    assert client.get_connection_info().state == ConnectionState.ERROR


# ---------------------------------------------------------------------------
# TransportRouter.refresh_connection_info: bounded, never raises, never queues
# ---------------------------------------------------------------------------


class _Active:
    def __init__(self, raises=False):
        self.refreshed = []
        self.raises = raises

    def refresh_connection_info(self, *, timeout):
        self.refreshed.append(timeout)
        if self.raises:
            raise TransportError(TransportErrorCode.ADAPTER_UNAVAILABLE, "gone")


def test_router_refresh_delegates_when_idle():
    active = _Active()
    assert TransportRouter(active).refresh_connection_info(timeout=5.0) is True
    assert active.refreshed == [5.0]


def test_router_refresh_is_skipped_while_the_lock_is_busy():
    active = _Active()
    router = TransportRouter(active)
    router._lock.acquire()  # a switch/connect/send is in flight
    try:
        start = time.monotonic()
        assert router.refresh_connection_info() is False
        assert time.monotonic() - start < 1.0
    finally:
        router._lock.release()
    assert active.refreshed == []


def test_router_refresh_swallows_errors_and_unsupported_transports():
    assert TransportRouter(_Active(raises=True)).refresh_connection_info() is False
    assert TransportRouter(object()).refresh_connection_info() is False


# ---------------------------------------------------------------------------
# server.py: descriptor from the accepted profile; worker refresh
# ---------------------------------------------------------------------------


@pytest.fixture
def accepted(server_module):
    original = server_module.instance_manager.get()

    def _set(radio):
        updated = dict(server_module.instance_manager.get())
        updated["radio"] = radio
        server_module.INSTANCE_IDENTITY = server_module.instance_manager.save(updated)

    yield _set
    server_module.instance_manager.save(original)
    server_module.INSTANCE_IDENTITY = original


def test_descriptor_comes_from_the_accepted_tcp_profile(server_module, accepted):
    accepted({"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}})

    descriptor = server_module.resolve_reconnect_descriptor("tcp")

    assert (descriptor.type, descriptor.address) == (ConnectionType.TCP, "192.168.2.34:4403")


def test_descriptor_comes_from_the_accepted_bluetooth_profile(server_module, accepted):
    accepted({
        "node_id": "!756f9960", "transport": "bluetooth",
        "endpoint": {"address": "3C:DC:75:6F:99:61", "label": "TAP2"},
    })

    descriptor = server_module.resolve_reconnect_descriptor("bluetooth")

    assert (descriptor.type, descriptor.address, descriptor.label) == (
        ConnectionType.BLUETOOTH, "3C:DC:75:6F:99:61", "TAP2"
    )


def test_serial_never_gets_a_descriptor(server_module, accepted):
    accepted({"node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}})

    assert server_module.resolve_reconnect_descriptor("serial") is None


def test_falls_back_to_the_routers_last_endpoint_when_the_profile_is_for_another_transport(
    server_module, accepted, monkeypatch
):
    accepted({"node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}})
    live = _tcp("192.168.2.99:4403")
    monkeypatch.setattr(
        server_module.transport_router, "get_connection_info",
        lambda: ConnectionInfo(state=ConnectionState.ERROR, descriptor=live, node_id=None),
    )

    assert server_module.resolve_reconnect_descriptor("tcp") is live


def test_no_known_endpoint_yields_none(server_module, accepted, monkeypatch):
    accepted({"node_id": "!067a40fa", "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}})
    monkeypatch.setattr(
        server_module.transport_router, "get_connection_info",
        lambda: ConnectionInfo(state=ConnectionState.ERROR, descriptor=_tcp(":4403"), node_id=None),  # respawned adapter, no host
    )

    assert server_module.resolve_reconnect_descriptor("tcp") is None


def test_health_worker_refresh_only_for_non_serial(server_module, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server_module.transport_router, "refresh_connection_info",
        lambda timeout=5.0: calls.append(timeout) or True,
    )

    assert server_module.refresh_non_serial_connection_state("serial") is False
    assert calls == []
    assert server_module.refresh_non_serial_connection_state("tcp") is True
    assert server_module.refresh_non_serial_connection_state("bluetooth") is True
    assert len(calls) == 2


def test_health_worker_source_refreshes_before_reading_the_state():
    """The worker loop itself is a `while True: sleep(30)`; pin the wiring."""
    import inspect
    import server

    source = inspect.getsource(server.radio_health_worker)
    assert "refresh_non_serial_connection_state(active_transport)" in source
    assert source.index("refresh_non_serial_connection_state") < source.index("compute_radio_health_status(")


def test_autorecovery_reconnects_with_the_accepted_endpoint(server_module, accepted, monkeypatch):
    accepted({"node_id": "!1fa065f0", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}})
    calls = []
    monkeypatch.setattr(server_module.transport_router, "reconnect", lambda **k: calls.append(k))
    monkeypatch.setattr(server_module, "log_system_event", lambda *a, **k: None)
    monkeypatch.setattr(server_module, "refresh_identity_after_reconnect", lambda *a, **k: None)
    monkeypatch.setattr(
        server_module.transport_router, "get_connection_info",
        lambda: ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=_tcp(), node_id="!1fa065f0"),
    )

    class _Sync:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(server_module.threading, "Thread", _Sync)
    state = server_module.transport_recovery_state
    saved = dict(state)
    state.update({"consecutive_bad_cycles": 0, "attempts": [], "in_progress": False,
                  "last_enabled": None, "limit_logged": False})
    saved_result = server_module.RADIO_IDENTITY_RESULT
    server_module.RADIO_IDENTITY_RESULT = {"status": "MATCH", "detected": {}, "error": None}
    with server_module.state_lock:
        server_module.settings["tcp_ble_autorecovery"] = {"enabled": True}
    try:
        for i in range(server_module.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES):
            server_module.process_transport_autorecovery("ERROR", "tcp", float(i * 30))
    finally:
        state.clear()
        state.update(saved)
        server_module.RADIO_IDENTITY_RESULT = saved_result

    assert len(calls) == 1
    assert calls[0]["descriptor"].address == "192.168.2.34:4403"
    assert calls[0]["timeout"] == server_module.TRANSPORT_RECONNECT_TIMEOUT_S
