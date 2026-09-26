"""A TCP session to a radio whose identity was refused (MISMATCH / NOT_FOUND)
must be closed, not left connected.

Before: detect_tcp_radio_identity() kept a successful probe connected,
verify_radio_identity() never closed it on MISMATCH, and
refresh_identity_after_reconnect() only recorded the status - so a wrong radio
stayed connected, held the radio's single client slot, and was reachable by
every sender that does not consult is_radio_available() (the MCA
AttachmentsService sends through transport_router directly).
"""
import pytest

from meshsrv.radio_transport import (
    ConnectionInfo,
    ConnectionState,
    SendResult,
    TransportError,
    TransportErrorCode,
)
from meshsrv.transport_router import TransportRouter

ACCEPTED = "!1fa065f0"


class _FakeTcp:
    """Enough of a TCP transport: connected until disconnect() is called,
    and a send on a closed session fails not_connected like the real one."""

    def __init__(self, disconnect_raises=False):
        self.connected = True
        self.disconnects = []
        self.sends = 0
        self.disconnect_raises = disconnect_raises

    def disconnect(self, *, timeout=15.0):
        self.disconnects.append(timeout)
        if self.disconnect_raises:
            raise TransportError(TransportErrorCode.TIMEOUT, "disconnect timed out")
        self.connected = False

    def get_connection_info(self):
        return ConnectionInfo(
            state=ConnectionState.CONNECTED if self.connected else ConnectionState.DISCONNECTED,
            descriptor=None,
            node_id=None,
        )

    def send_text(self, message, *, timeout=15.0):
        if not self.connected:
            raise TransportError(TransportErrorCode.NOT_CONNECTED, "TCPTransport is not connected")
        self.sends += 1
        return SendResult(accepted=True, packet_id=1)


@pytest.fixture
def env(server_module, monkeypatch):
    s = server_module
    original_identity = s.instance_manager.get()
    original_result = s.RADIO_IDENTITY_RESULT

    updated = dict(original_identity)
    updated["radio"] = {"node_id": ACCEPTED, "transport": "tcp", "endpoint": {"host": "192.168.2.34", "port": 4403}}
    s.INSTANCE_IDENTITY = s.instance_manager.save(updated)

    tcp = _FakeTcp()
    monkeypatch.setattr(s, "tcp_ipc_transport", tcp)
    events = []
    monkeypatch.setattr(
        s, "log_system_event",
        lambda title, level="INFO", details="", source="system": events.append((title, level, details)),
    )
    yield s, tcp, events

    s.instance_manager.save(original_identity)
    s.INSTANCE_IDENTITY = original_identity
    s.RADIO_IDENTITY_RESULT = original_result


# --- refresh_identity_after_reconnect (manual + auto reconnect paths) ----


def test_mismatch_after_reconnect_closes_the_session_and_reports_it(env):
    s, tcp, events = env

    status = s.refresh_identity_after_reconnect("tcp", {"node_id": "!deadbeef"})

    assert status == "MISMATCH"
    assert tcp.disconnects == [10]
    assert tcp.connected is False
    assert any(t == "Disconnected from a radio that failed identity verification" and lvl == "WARNING"
               for t, lvl, _ in events)


def test_not_found_after_reconnect_closes_the_session(env):
    s, tcp, _ = env

    status = s.refresh_identity_after_reconnect("tcp", {"node_id": None})

    assert status == "NOT_FOUND"
    assert tcp.connected is False


def test_match_after_reconnect_leaves_the_session_alone(env):
    s, tcp, events = env

    assert s.refresh_identity_after_reconnect("tcp", {"node_id": ACCEPTED}) == "MATCH"
    assert tcp.disconnects == [] and tcp.connected is True
    assert events == []


def test_non_tcp_is_untouched(env):
    s, tcp, _ = env

    assert s.refresh_identity_after_reconnect("bluetooth", {"node_id": "!deadbeef"}) is None
    assert s.refresh_identity_after_reconnect("serial", {"node_id": "!deadbeef"}) is None
    assert tcp.disconnects == []


def test_a_failing_disconnect_does_not_mask_the_identity_result(env, monkeypatch):
    s, _, events = env
    monkeypatch.setattr(s, "tcp_ipc_transport", _FakeTcp(disconnect_raises=True))

    assert s.refresh_identity_after_reconnect("tcp", {"node_id": "!deadbeef"}) == "MISMATCH"
    assert "disconnect failed" in events[-1][2]


def test_after_teardown_a_send_through_the_router_cannot_reach_the_wrong_radio(env, monkeypatch):
    """The point of the exercise: MCA sends through transport_router with no
    is_radio_available() check. With the session closed the send fails."""
    s, tcp, _ = env
    router = TransportRouter(tcp)
    monkeypatch.setattr(s, "transport_router", router)
    assert router.send_text(object()).accepted is True  # before: the wrong radio would answer

    s.refresh_identity_after_reconnect("tcp", {"node_id": "!deadbeef"})

    with pytest.raises(TransportError) as excinfo:
        router.send_text(object())
    assert excinfo.value.code == TransportErrorCode.NOT_CONNECTED
    assert tcp.sends == 1


# --- verify_radio_identity (boot + background identity retry) ------------


def _detect_result(status, node_id):
    detected = {"node_id": node_id, "long_name": "X"} if node_id else {}
    return ({
        "status": status, "checked_at": "2026-09-26T00:00:00+00:00", "configured": {},
        "detected": detected, "error": None if node_id else "boom",
        "error_code": None if node_id else "connect_failed",
    }, "")


@pytest.mark.parametrize("node_id,expected_teardown", [
    ("!deadbeef", True),      # a different radio answered: MISMATCH
    (ACCEPTED, False),        # the accepted radio: MATCH
    (None, False),            # never reached a radio: DETECTION_ERROR (detect already closed its own session)
])
def test_boot_identity_check_closes_only_a_refused_session(env, monkeypatch, node_id, expected_teardown):
    s, tcp, _ = env
    monkeypatch.setattr(s, "TCP_IDENTITY_BOOT_RETRY_DELAYS_S", ())
    monkeypatch.setattr(
        s, "detect_tcp_radio_identity",
        lambda transport, host, port, timeout=25: _detect_result("MATCH" if node_id else "DETECTION_ERROR", node_id),
    )

    s.verify_radio_identity()

    assert (tcp.disconnects != []) is expected_teardown
    if node_id == "!deadbeef":
        assert s.RADIO_IDENTITY_RESULT["status"] == "MISMATCH"


def test_boot_check_never_tears_down_a_serial_radio(env, monkeypatch):
    s, tcp, _ = env
    updated = dict(s.instance_manager.get())
    updated["radio"] = {"node_id": ACCEPTED, "transport": "serial", "endpoint": {"port": "/dev/ttyACM0"}}
    s.INSTANCE_IDENTITY = s.instance_manager.save(updated)
    monkeypatch.setattr(
        s, "detect_radio_identity", lambda cmd, port, timeout=25: _detect_result("MATCH", "!deadbeef")
    )

    s.verify_radio_identity()

    assert s.RADIO_IDENTITY_RESULT["status"] == "MISMATCH"
    assert tcp.disconnects == []


# --- autorecovery reports it ------------------------------------------------


def test_autorecovery_logs_an_error_when_the_reconnect_reached_a_refused_radio(env, monkeypatch):
    s, tcp, events = env
    monkeypatch.setattr(s.transport_router, "reconnect", lambda **k: None)
    monkeypatch.setattr(
        s.transport_router, "get_connection_info",
        lambda: ConnectionInfo(state=ConnectionState.CONNECTED, descriptor=None, node_id="!deadbeef"),
    )

    class _Sync:
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(s.threading, "Thread", _Sync)
    state = s.transport_recovery_state
    saved = dict(state)
    state.update({"consecutive_bad_cycles": 0, "attempts": [], "in_progress": False,
                  "last_enabled": None, "limit_logged": False})
    s.RADIO_IDENTITY_RESULT = {"status": "MATCH", "detected": {}, "error": None}
    with s.state_lock:
        s.settings["tcp_ble_autorecovery"] = {"enabled": True}
    try:
        for i in range(s.TRANSPORT_RECOVERY_CONSECUTIVE_CYCLES):
            s.process_transport_autorecovery("ERROR", "tcp", float(i * 30))
    finally:
        state.clear()
        state.update(saved)

    assert tcp.connected is False
    assert any("failed identity verification" in title and lvl == "ERROR" for title, lvl, _ in events)


# --- /api/meshtastic/reconnect ---------------------------------------------


def test_manual_reconnect_route_reports_a_refused_radio_as_409(env, monkeypatch):
    import threading as _threading
    from flask import Flask
    from api.api_meshtastic import register_meshtastic_routes
    import test_api_meshtastic as helpers  # sibling test module's fakes (tests/ is on sys.path)

    tcp = helpers._FakeTcpTransport()
    router = TransportRouter(tcp)
    app = Flask(__name__)
    register_meshtastic_routes(
        app, helpers._handle_errors, _threading.Lock(), {"meshtastic": {"transport": "tcp"}}, lambda: None,
        router, helpers._FakeSerialTransport(), helpers._FakeBleTransport(bad_address="x"), tcp,
        "/dev/ttyACM0", "!756f9960", helpers._FakeSerialTransport(listener_pid=1),
        helpers._FakeInstanceManager(),
        lambda active, detected: "MISMATCH",
    )

    response = app.test_client().post("/api/meshtastic/reconnect")

    assert response.status_code == 409
    body = response.get_json()
    assert body["ok"] is False and body["error_code"] == "identity_mismatch"
    assert body["identity_status"] == "MISMATCH"
