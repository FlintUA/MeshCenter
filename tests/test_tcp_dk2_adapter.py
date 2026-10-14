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
