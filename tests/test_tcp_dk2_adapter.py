"""DK2 adapter-side behaviour: kernel liveness options on the TCP socket, the
library's silent self-reconnect disabled, and the PKI-DM channel index."""
import socket
import types

import pytest

from adapters.meshtastic import tcp_transport as tcp_mod
from adapters.meshtastic.tcp_transport import TCPTransport
from meshsrv.radio_transport import ReceivedTextEvent


class FakeSocket:
    def __init__(self):
        self.options = {}
        self.timeout = "unset"

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, level, name, value):
        self.options[(level, name)] = value

    def close(self):
        pass


def test_open_socket_enables_keepalive_and_user_timeout(monkeypatch):
    fake = FakeSocket()
    monkeypatch.setattr(tcp_mod.socket, "create_connection", lambda *a, **k: fake)
    result = TCPTransport(host="10.0.0.5")._open_socket(5.0)

    assert result is fake and fake.timeout is None, "back to blocking mode, as before"
    assert fake.options[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
    for attr, expected in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3),
                           ("TCP_USER_TIMEOUT", 60_000)):
        if hasattr(socket, attr):  # hasattr-guarded: Linux has all four
            assert fake.options[(socket.IPPROTO_TCP, getattr(socket, attr))] == expected


def test_unsupported_socket_options_never_break_connect(monkeypatch):
    class Picky(FakeSocket):
        def setsockopt(self, level, name, value):
            raise OSError("not supported here")

    monkeypatch.setattr(tcp_mod.socket, "create_connection", lambda *a, **k: Picky())
    assert TCPTransport(host="10.0.0.5")._open_socket(5.0) is not None


def test_a_lost_link_is_never_reported_healthy():
    iface = types.SimpleNamespace(_link_lost=True, _rxThread=None, isConnected=None)
    assert TCPTransport._interface_is_healthy(iface) is False
    assert TCPTransport._interface_is_healthy(types.SimpleNamespace()) is True


def test_library_self_reconnect_is_disabled():
    """The override lives in a class built inside _open_interface(); check the
    source wiring (the live behaviour is covered by the fake-radio smoke)."""
    import inspect
    source = inspect.getsource(TCPTransport._open_interface)
    assert "def _reconnect(self):" in source
    assert "self._link_lost = True" in source and "self._wantExit = True" in source


def _packet(**extra):
    packet = {"from": 0x1fa065f0, "to": 0xb0f14d2a, "id": 7,
              "decoded": {"portnum": "TEXT_MESSAGE_APP", "payload": b"x", "text": "hi"}}
    packet.update(extra)
    return packet


def _event(packet):
    iface = types.SimpleNamespace(myInfo=types.SimpleNamespace(my_node_num=0xb0f14d2a))
    event = TCPTransport(host="10.0.0.5")._normalize_text(packet, iface)
    assert isinstance(event, ReceivedTextEvent)
    return event


def test_pki_dm_without_a_channel_reports_unknown_not_zero():
    assert _event(_packet(pkiEncrypted=True)).channel_index is None


def test_ordinary_packets_keep_their_channel_semantics():
    assert _event(_packet()).channel_index == 0            # omitted default 0, not PKI
    assert _event(_packet(channel=1)).channel_index == 1
    assert _event(_packet(channel=2, pkiEncrypted=True)).channel_index == 2


# ===========================================================================
# DK3 - real-radio findings: the T-Beam closes API sessions on its own
# ===========================================================================
import inspect  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from adapters.meshtastic.fake_radio_server import FakeMeshtasticTcpServer  # noqa: E402
from meshsrv.radio_transport import ConnectionDescriptor, ConnectionState, ConnectionType, TransportError  # noqa: E402


def _descriptor(port):
    return ConnectionDescriptor(type=ConnectionType.TCP, address=f"127.0.0.1:{port}")


def _wait(predicate, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _live_timers():
    return [t for t in threading.enumerate() if isinstance(t, threading.Timer) and t.is_alive()]


def test_radio_fin_is_answered_by_our_close_within_two_seconds_and_state_is_not_connected():
    logs = []
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port, on_log=lambda m, l="INFO": logs.append((l, m)))
    try:
        transport.connect(_descriptor(server.port), timeout=10.0)
        assert _wait(lambda: server.active_connections == 1, 2)
        assert server.drop_clients() == 1                      # the radio closes its side first
        started = time.monotonic()
        assert _wait(lambda: server.active_connections == 0, 2.0), "our FIN must answer the radio's within 2 s"
        assert time.monotonic() - started < 2.0
        assert _wait(lambda: transport.get_connection_info().state != ConnectionState.CONNECTED, 2.0), \
            "never 'connected' while no session exists"
        assert any("TCP session lost" in m and "age=" in m and "rx=" in m and "tx=" in m for _, m in logs), logs
    finally:
        transport.close()
        server.shutdown()


def test_failed_handshake_releases_the_radios_slot_immediately():
    """DK3: a bare close() on a socket another thread is blocked reading kept the
    connection (and the radio's single slot) until the radio gave up at 120 s."""
    server = FakeMeshtasticTcpServer(complete_handshake=False)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        with pytest.raises(TransportError):
            transport.connect(_descriptor(server.port), timeout=6.0)
        assert _wait(lambda: server.active_connections == 0, 2.0), "slot must be free right after the failure"
    finally:
        transport.close()
        server.shutdown()


def test_no_heartbeat_is_armed_or_sent_after_close():
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        transport.connect(_descriptor(server.port), timeout=10.0)
        interface = transport._interface
        assert interface.heartbeatTimer is not None, "armed while the session is up"
        transport.disconnect(timeout=10.0)
        assert _wait(lambda: not _live_timers(), 2.0)
        interface._startHeartbeat()                 # a late handshake completing after close()
        assert not _live_timers(), "a closed interface must never re-arm the heartbeat"
        assert interface.heartbeatTimer is None
    finally:
        transport.close()
        server.shutdown()


def test_uptime_going_backwards_is_logged_as_a_radio_reboot():
    logs = []
    transport = TCPTransport(host="127.0.0.1", on_log=lambda m, l="INFO": logs.append((l, m)))
    transport._note_radio_uptime(5000, "at connect")
    transport._note_radio_uptime(5100, "local telemetry")
    assert not [m for _, m in logs if "REBOOTED" in m]
    transport._note_radio_uptime(40, "at connect")
    assert [m for l, m in logs if "REBOOTED" in m and l == "WARNING"]


def test_installed_meshtastic_still_has_every_private_hook_the_adapter_relies_on():
    """The adapter overrides/reads private members of a pinned third-party
    library (meshtastic>=2.7.9,<2.8). A version bump that renames one must fail
    HERE, in CI, not as a silent no-op on a user's radio."""
    from meshtastic import NODELESS_WANT_CONFIG_ID
    from meshtastic.mesh_interface import MeshInterface
    from meshtastic.stream_interface import StreamInterface
    from meshtastic.tcp_interface import TCPInterface

    for owner, names in (
        (TCPInterface, ("_reconnect", "myConnect", "_readBytes", "_writeBytes", "_socket_shutdown", "close")),
        (StreamInterface, ("_waitConnected", "_sendToRadioImpl")),
        (MeshInterface, ("_startHeartbeat", "sendHeartbeat", "_startConfig", "_connected", "_disconnected",
                         "_handleFromRadio", "close")),
    ):
        for name in names:
            assert callable(getattr(owner, name, None)), f"{owner.__name__}.{name} is gone"
    assert NODELESS_WANT_CONFIG_ID == 69420
    assert "noNodes" in inspect.signature(TCPInterface.__init__).parameters
    source = inspect.getsource(MeshInterface.__init__)
    for attribute in ("heartbeatTimer", "isConnected", "nodesByNum", "noNodes"):
        assert f"self.{attribute}" in source, f"MeshInterface no longer sets {attribute}"
    reconnect = inspect.getsource(TCPInterface._reconnect)
    assert "myConnect" in reconnect, "the library's self-reconnect changed shape - re-check our override"


def test_first_connect_asks_for_the_full_config_reconnects_for_config_only():
    """Measured on the real T-Beam: 13999 B / 100 nodes -> 1187 B / own node only,
    identity identical. Boot (first session in this process) still needs nodes."""
    server = FakeMeshtasticTcpServer(complete_handshake=True)
    transport = TCPTransport(host="127.0.0.1", port=server.port)
    try:
        transport.connect(_descriptor(server.port), timeout=10.0)
        assert transport._interface.noNodes is False
        assert server.accepted_want_config_id != 69420
        transport.connect(_descriptor(server.port), force=True, timeout=10.0)
        assert transport._interface.noNodes is True
        assert _wait(lambda: server.accepted_want_config_id == 69420, 2.0), server.accepted_want_config_id
        assert transport.get_connection_info().state == ConnectionState.CONNECTED
    finally:
        transport.close()
        server.shutdown()
