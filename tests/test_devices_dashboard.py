"""Tests for server.py's api_devices_dashboard() (Radio Profiles &
Connections Model, PR 2): the `connection` block must reflect whichever
transport is ACTUALLY active (via meshsrv/connection_status.py's
connection_payload(), TransportRouter's own live state), not
RadioConnectionManager's serial-only "release the port for an external
app" status shown unconditionally regardless of transport - the live-
caught bug this PR fixes (a genuinely-connected TCP/Bluetooth radio
previously always showed serial-flavored status, specifically "listener
stopped", because radio_health's listener_running flag - what
RadioConnectionManager.status() was fed - only ever tracks the SERIAL
listener thread, which never even starts when a non-serial transport is
active).

Uses the server_module fixture + Flask's test_request_context() to call
api_devices_dashboard() directly, same convention as
tests/test_node_manager_radio_transport_aware.py (no test in this suite
drives server.py's own routes through app.test_client()).
"""
import pytest

from meshsrv.radio_transport import ConnectionDescriptor, ConnectionInfo, ConnectionState, ConnectionType, TransportError, TransportErrorCode


class _FakeTransportRouter:
    """Duck-typed stand-in for TransportRouter - only get_connection_info()
    is exercised by api_devices_dashboard()."""

    def __init__(self, info):
        self._info = info

    def get_connection_info(self):
        return self._info


def _set_accepted_radio(server_module, radio, active_profile_id=""):
    identity = server_module.instance_manager.get()
    updated = dict(identity)
    updated["radio"] = radio
    updated["active_profile_id"] = active_profile_id
    saved = server_module.instance_manager.save(updated)
    server_module.INSTANCE_IDENTITY = saved
    return saved


def _call_dashboard(server_module):
    with server_module.app.test_request_context("/api/devices/dashboard", method="GET"):
        result = server_module.api_devices_dashboard()
    if isinstance(result, tuple):
        response, status = result
    else:
        response, status = result, 200
    return response.get_json(), status


class _PreserveState:
    """Snapshots/restores instance identity AND transport_router/radio_health
    around each test - this suite monkeypatches all three."""

    def __init__(self, server_module):
        self.server_module = server_module
        self.original_identity = server_module.instance_manager.get()
        self.original_router = server_module.transport_router
        self.original_listener_running = None

    def __enter__(self):
        with self.server_module.state_lock:
            self.original_listener_running = self.server_module.radio_health.get("listener_running")
        return self

    def __exit__(self, *exc):
        self.server_module.instance_manager.save(self.original_identity)
        self.server_module.INSTANCE_IDENTITY = self.original_identity
        self.server_module.transport_router = self.original_router
        with self.server_module.state_lock:
            self.server_module.radio_health["listener_running"] = self.original_listener_running


@pytest.fixture
def _preserve(server_module):
    with _PreserveState(server_module) as guard:
        yield guard


# ---------------------------------------------------------------------------
# TCP-active profile
# ---------------------------------------------------------------------------

def test_tcp_active_and_connected_reports_connected_not_serial_listener_stopped(server_module, _preserve, monkeypatch):
    """The actual reported bug: a genuinely-connected TCP radio must not
    show the serial-flavored "listener stopped" status."""
    _set_accepted_radio(server_module, {
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "port": "", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {"tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}}},
        "preferred_transport": "tcp", "last_successful_transport": "tcp",
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id="!1fa065f0", connected_since=1758636546.0)
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))
    with server_module.state_lock:
        server_module.radio_health["listener_running"] = False  # serial listener never starts for TCP

    data, status = _call_dashboard(server_module)

    assert status == 200
    assert data["ok"] is True
    assert data["connection"]["mode"] == "connected"
    assert data["connection"]["listener_running"] is True
    assert data["connection"]["type"] == "tcp"
    assert data["connection"]["address"] == "192.168.2.34:4403"


def test_tcp_active_and_connected_does_not_carry_serial_specific_fields_as_current_state(server_module, _preserve, monkeypatch):
    _set_accepted_radio(server_module, {
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "port": "", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id="!1fa065f0")
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))

    data, status = _call_dashboard(server_module)

    # No serial port device path leaking into a TCP profile's connection
    # block. (radio.port itself is a separate, pre-existing concern - see
    # InstanceManager._normalize()'s first_text() port-field fallback:
    # an explicit "" doesn't actually clear a stale prior value there,
    # unrelated to this PR's connection-status fix, not tested here.)
    assert data["connection"]["serial_port"] == ""


def test_tcp_active_and_disconnected_reports_a_real_non_connected_mode(server_module, _preserve, monkeypatch):
    _set_accepted_radio(server_module, {
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "port": "", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")
    info = ConnectionInfo(
        state=ConnectionState.ERROR, descriptor=descriptor, node_id=None,
        last_error=TransportError(TransportErrorCode.CONNECT_FAILED, "connection refused"),
    )
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))

    data, status = _call_dashboard(server_module)

    assert data["connection"]["mode"] == "error"
    assert data["connection"]["listener_running"] is False
    assert "connection refused" in data["connection"]["last_error"]


def test_tcp_connected_since_is_iso_formatted_not_a_raw_unix_float(server_module, _preserve, monkeypatch):
    """ConnectionInfo.connected_since is a raw time.time()-style float -
    the frontend's `new Date(value)` needs an ISO string (or milliseconds),
    same format the serial branch already produces, or it renders a
    garbled ~1970 date."""
    _set_accepted_radio(server_module, {
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "port": "", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id="!1fa065f0", connected_since=1758636546.0)
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))

    data, status = _call_dashboard(server_module)

    connected_since = data["connection"]["connected_since"]
    assert connected_since is not None
    assert not isinstance(connected_since, float)
    assert "T" in connected_since  # ISO-8601 marker


# ---------------------------------------------------------------------------
# Serial-active profile - must be byte-for-byte unchanged
# ---------------------------------------------------------------------------

def test_serial_active_connection_block_matches_pre_existing_shape(server_module, _preserve, monkeypatch):
    _set_accepted_radio(server_module, {
        "node_id": "!aabbccdd", "long_name": "Serial Radio",
        "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyACM0")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id=None)
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))
    with server_module.state_lock:
        server_module.radio_health["listener_running"] = True

    data, status = _call_dashboard(server_module)

    assert status == 200
    assert data["connection"]["type"] == "serial"
    # Exactly RadioConnectionManager's own vocabulary - unaffected by this PR.
    assert data["connection"]["mode"] == "connected"
    assert data["connection"]["listener_running"] is True
    assert "released" in data["connection"]
    assert "commands_allowed" in data["connection"]
    assert "message" in data["connection"]
    assert "serial_port" in data["connection"]


def test_serial_active_response_keys_unchanged_by_this_pr(server_module, _preserve, monkeypatch):
    """Contract guard: the exact key set of `connection` for the serial
    case must not change shape - the current frontend depends on it."""
    _set_accepted_radio(server_module, {
        "node_id": "!aabbccdd", "long_name": "Serial Radio",
        "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyACM0")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id=None)
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))

    data, _ = _call_dashboard(server_module)

    expected_keys = {
        "mode", "released", "commands_allowed", "listener_running", "message",
        "serial_port", "updated_at", "released_at", "last_error",
        "listener_pid", "connected_since", "type", "address",
    }
    assert expected_keys.issubset(set(data["connection"].keys()))


# ---------------------------------------------------------------------------
# radio.connections / preferred_transport / last_successful_transport (PR 1 data)
# ---------------------------------------------------------------------------

def test_radio_connections_and_preferred_transport_match_the_persisted_record(server_module, _preserve, monkeypatch):
    _set_accepted_radio(server_module, {
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "port": "", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
        "connections": {
            "tcp": {"endpoint": {"host": "192.168.2.34", "port": 4403}, "last_successful_at": "2026-09-23T12:00:00+00:00"},
            "serial": {"endpoint": {"port": "/dev/ttyACM0"}},
        },
        "preferred_transport": "tcp",
        "last_successful_transport": "tcp",
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id="!1fa065f0")
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))

    data, status = _call_dashboard(server_module)

    assert set(data["radio"]["connections"].keys()) == {"tcp", "serial"}
    assert data["radio"]["connections"]["tcp"]["endpoint"] == {"host": "192.168.2.34", "port": 4403}
    assert data["radio"]["preferred_transport"] == "tcp"
    assert data["radio"]["last_successful_transport"] == "tcp"


def test_radio_connections_synthesized_for_a_legacy_record_with_none_stored(server_module, _preserve, monkeypatch):
    """A profile predating PR 1 (flat transport/endpoint, no `connections`
    key at all) must still get a synthesized entry, not an empty/missing
    `connections`."""
    _set_accepted_radio(server_module, {
        "node_id": "!aabbccdd", "long_name": "Serial Radio",
        "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyACM0")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id=None)
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))

    data, status = _call_dashboard(server_module)

    assert data["radio"]["connections"] == {"serial": {"endpoint": {"port": "/dev/ttyACM0"}}}
    assert data["radio"]["preferred_transport"] == "serial"


# ---------------------------------------------------------------------------
# Identity-safety fields - must survive the refactor unchanged
# ---------------------------------------------------------------------------

def test_identity_fields_are_not_lost_by_the_refactor(server_module, _preserve, monkeypatch):
    _set_accepted_radio(server_module, {
        "node_id": "!1fa065f0", "long_name": "T-Beam",
        "port": "", "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403},
    })
    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address="192.168.2.34:4403")
    info = ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=descriptor, node_id="!1fa065f0")
    monkeypatch.setattr(server_module, "transport_router", _FakeTransportRouter(info))
    monkeypatch.setattr(server_module, "RADIO_IDENTITY_RESULT", {
        "status": "MATCH", "checked_at": "2026-09-23T12:00:00+00:00",
        "configured": {}, "detected": {}, "error": None,
    })

    data, status = _call_dashboard(server_module)

    assert data["radio"]["identity_status"] == "MATCH"
    assert data["radio"]["identity_checked_at"] == "2026-09-23T12:00:00+00:00"
