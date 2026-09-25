"""Tests for adapters/meshtastic/tcp_transport.py - the explicit
DISCONNECTED -> CONNECTING -> TCP_CONNECTED -> SYNCING -> READY state
machine, the dns_error/connect_refused/connect_timeout/tcp_connected/
protocol_sync_timeout/identity_mismatch/remote_disconnect diagnostic
split, and the jittered reconnect backoff.

Everything here runs against a fake socket layer (monkeypatched
socket.create_connection) and a fake meshtastic.tcp_interface.TCPInterface
(mocked at the module boundary, same technique tests/test_ble_transport.py
already uses for meshtastic.ble_interface) - no real network I/O, no
hardware needed. The acceptance criteria against a real T-Beam
(192.168.2.34:4403, positive/regression firmware references) are a
separate, documented manual test procedure - not reproduced here.
"""
import socket
import sys
import threading
import time
import types

import pytest

from adapters.meshtastic.tcp_transport import (
    DEFAULT_TCP_PORT,
    TCPTransport,
    _parse_host_port,
)
from meshsrv.radio_transport import (
    ConnectionDescriptor,
    ConnectionState,
    ConnectionType,
    OutgoingMessage,
    TransportError,
    TransportErrorCode,
)


# ---------------------------------------------------------------------------
# _parse_host_port
# ---------------------------------------------------------------------------

def test_parse_host_port_with_explicit_port():
    assert _parse_host_port("192.168.2.34:4403", default_port=9999) == ("192.168.2.34", 4403)


def test_parse_host_port_bare_host_uses_default_port():
    assert _parse_host_port("192.168.2.34", default_port=4403) == ("192.168.2.34", 4403)


def test_parse_host_port_bracketed_ipv6_with_port():
    assert _parse_host_port("[::1]:4403", default_port=9999) == ("::1", 4403)


def test_parse_host_port_bracketed_ipv6_without_port_uses_default():
    assert _parse_host_port("[::1]", default_port=4403) == ("::1", 4403)


def test_parse_host_port_empty_string():
    assert _parse_host_port("", default_port=4403) == ("", 4403)


def test_parse_host_port_hostname_with_port():
    assert _parse_host_port("radio.local:4403", default_port=9999) == ("radio.local", 4403)


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------

class _FakeMyInfo:
    def __init__(self, my_node_num):
        self.my_node_num = my_node_num


class _FakePacket:
    id = 12345


class _FakeReaderThread:
    """Stands in for StreamInterface's own self._rxThread - `alive`
    controls what is_alive() reports AFTER the fake interface's close()
    has already been called, simulating whether the library's own
    GRACEFUL_CLOSE_TIMEOUT-bounded wait actually succeeded in stopping
    it (see _detach_and_close_async()'s own hard-close-fallback logic,
    the thing under test in the "reader thread survives close()"
    section below)."""

    def __init__(self, alive: bool):
        self._alive = alive

    def is_alive(self) -> bool:
        return self._alive


class _FakeTCPInterface:
    """Stands in for meshtastic.tcp_interface.TCPInterface. Records
    close() calls and the hostname/port it was constructed with, and can
    be told to raise/hang on construction to simulate the acceptance
    tests' regression firmware. `rx_thread_survives_close` (class-level,
    defaults False = the common/expected case) controls whether the fake
    reader thread reports itself still alive() after close() - simulates
    the live-caught pixel-111 gap where the real library's own close()
    doesn't always actually stop it."""

    instances = []
    construct_delay_s = 0.0
    construct_exception = None
    rx_thread_survives_close = False

    def __init__(self, hostname, portNumber=DEFAULT_TCP_PORT, connectNow=True):
        if _FakeTCPInterface.construct_delay_s:
            time.sleep(_FakeTCPInterface.construct_delay_s)
        if _FakeTCPInterface.construct_exception is not None:
            raise _FakeTCPInterface.construct_exception
        self.hostname = hostname
        self.portNumber = portNumber
        self.myInfo = _FakeMyInfo(my_node_num=0x756F9960)
        self.nodes = {}
        self.localNode = types.SimpleNamespace(nodeNum=0x756F9960, channels=[])
        self.metadata = None
        self.closed = False
        self._rxThread = _FakeReaderThread(alive=_FakeTCPInterface.rx_thread_survives_close)
        self.socket = _FakeSocket()
        _FakeTCPInterface.instances.append(self)

    def sendText(self, **kwargs):
        return _FakePacket()

    def sendData(self, *args, **kwargs):
        return _FakePacket()

    def sendWaypoint(self, **kwargs):
        return _FakePacket()

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _fake_tcp_interface_module(monkeypatch):
    _FakeTCPInterface.instances = []
    _FakeTCPInterface.construct_delay_s = 0.0
    _FakeTCPInterface.construct_exception = None
    _FakeTCPInterface.rx_thread_survives_close = False
    fake_module = types.ModuleType("meshtastic.tcp_interface")
    fake_module.TCPInterface = _FakeTCPInterface
    monkeypatch.setitem(sys.modules, "meshtastic.tcp_interface", fake_module)
    yield
    monkeypatch.delitem(sys.modules, "meshtastic.tcp_interface", raising=False)


class _FakeSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_create_connection(monkeypatch, *, result=None, exception=None):
    """Patches socket.create_connection as used by
    TCPTransport._probe_tcp_reachable() - either returns a fresh
    _FakeSocket() (default `result`) or raises `exception`."""

    def _fake_create_connection(address, timeout=None):
        if exception is not None:
            raise exception
        return result if result is not None else _FakeSocket()

    monkeypatch.setattr(
        "adapters.meshtastic.tcp_transport.socket.create_connection", _fake_create_connection
    )


def _descriptor(address="192.168.2.34:4403"):
    return ConnectionDescriptor(type=ConnectionType.TCP, address=address)


# ---------------------------------------------------------------------------
# Happy path / state machine
# ---------------------------------------------------------------------------

def test_connect_happy_path_reaches_ready(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")

    info = transport.connect(_descriptor(), timeout=5)

    assert info.state == ConnectionState.CONNECTED
    assert info.node_id == "!756f9960"
    assert transport.is_connected()
    assert transport.internal_state == "ready"
    assert len(_FakeTCPInterface.instances) == 1
    assert _FakeTCPInterface.instances[0].hostname == "192.168.2.34"
    assert _FakeTCPInterface.instances[0].portNumber == DEFAULT_TCP_PORT


def test_connect_uses_port_from_descriptor_address(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="placeholder", port=9999)

    transport.connect(_descriptor("192.168.2.34:4403"), timeout=5)

    assert _FakeTCPInterface.instances[0].hostname == "192.168.2.34"
    assert _FakeTCPInterface.instances[0].portNumber == 4403


def test_connect_unsupported_descriptor_type_raises_without_opening_anything(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(ConnectionDescriptor(type=ConnectionType.SERIAL, address="/dev/ttyACM0"), timeout=5)

    assert excinfo.value.code == TransportErrorCode.UNSUPPORTED
    assert len(_FakeTCPInterface.instances) == 0


def test_send_text_reuses_the_same_interface_not_a_new_one(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)

    transport.send_text(OutgoingMessage(text="hi", destination_id="^all"), timeout=5)
    transport.send_text(OutgoingMessage(text="hi again", destination_id="^all"), timeout=5)

    assert len(_FakeTCPInterface.instances) == 1


def test_send_text_before_connect_returns_not_connected_without_opening_interface():
    transport = TCPTransport(host="192.168.2.34")

    result = transport.send_text(OutgoingMessage(text="hi", destination_id="^all"), timeout=5)

    assert result.accepted is False
    assert result.error.code == TransportErrorCode.NOT_CONNECTED
    assert len(_FakeTCPInterface.instances) == 0


def test_get_nodes_before_connect_raises_not_connected():
    transport = TCPTransport(host="192.168.2.34")

    with pytest.raises(TransportError) as excinfo:
        transport.get_nodes(timeout=5)
    assert excinfo.value.code == TransportErrorCode.NOT_CONNECTED


# ---------------------------------------------------------------------------
# Diagnostics - the dns_error/connect_refused/connect_timeout/
# tcp_connected/protocol_sync_timeout split.
# ---------------------------------------------------------------------------

def test_connect_dns_failure_reports_dns_error(monkeypatch):
    _patch_create_connection(monkeypatch, exception=socket.gaierror("Name or service not known"))
    transport = TCPTransport(host="no-such-radio.invalid")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor("no-such-radio.invalid:4403"), timeout=5)

    assert excinfo.value.code == TransportErrorCode.DNS_ERROR
    assert transport.internal_state == "error"
    assert len(_FakeTCPInterface.instances) == 0  # never even got to the protocol layer


def test_connect_refused_reports_connect_refused(monkeypatch):
    _patch_create_connection(monkeypatch, exception=ConnectionRefusedError("refused"))
    transport = TCPTransport(host="192.168.2.34")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(), timeout=5)

    assert excinfo.value.code == TransportErrorCode.CONNECT_REFUSED
    assert len(_FakeTCPInterface.instances) == 0


def test_connect_probe_timeout_reports_connect_timeout(monkeypatch):
    _patch_create_connection(monkeypatch, exception=TimeoutError("timed out"))
    transport = TCPTransport(host="192.168.2.34")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(), timeout=5)

    assert excinfo.value.code == TransportErrorCode.CONNECT_TIMEOUT
    assert len(_FakeTCPInterface.instances) == 0


def test_connect_immediate_protocol_rejection_reports_tcp_connected(monkeypatch):
    """TCP is proven reachable (the probe succeeds), but the Meshtastic
    protocol layer raises synchronously rather than hanging - e.g.
    something other than a Meshtastic radio is listening on that port.
    Must be distinguishable from protocol_sync_timeout (a hang)."""
    _patch_create_connection(monkeypatch)
    _FakeTCPInterface.construct_exception = ValueError("not a Meshtastic frame")
    transport = TCPTransport(host="192.168.2.34")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(), timeout=5)

    assert excinfo.value.code == TransportErrorCode.TCP_CONNECTED
    assert transport.internal_state == "error"


def test_connect_regression_firmware_hang_reports_protocol_sync_timeout_cleanly(monkeypatch):
    """The acceptance-test regression scenario (firmware 2.7.26.54e0d8d:
    TCP connects, FromRadio packets partially received, config never
    completes) modeled as: the raw probe succeeds (radio accepts the TCP
    connection) but the protocol handshake (TCPInterface's own blocking
    constructor) never returns. Must be reported cleanly as
    protocol_sync_timeout within the caller's declared budget - never an
    indefinite hang, never the generic ambiguous TIMEOUT code."""
    _patch_create_connection(monkeypatch)
    _FakeTCPInterface.construct_delay_s = 3600  # would hang "forever" if not bounded
    transport = TCPTransport(host="192.168.2.34")

    started = time.monotonic()
    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(), timeout=1.0)
    elapsed = time.monotonic() - started

    assert excinfo.value.code == TransportErrorCode.PROTOCOL_SYNC_TIMEOUT
    assert transport.internal_state == "error"
    # Reported at/near the declared budget, not anywhere close to the
    # simulated 3600s "hang" - the whole point of tier-1 enforcement.
    assert elapsed < 5.0
    # The abandoned background thread is still "connecting" in the
    # background per the tier-1/tier-2 gap documented on the
    # RadioTransport ABC itself - the caller was released regardless.


def test_identity_mismatch_closes_the_interface_before_raising(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34", expected_node_id="!deadbeef")

    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor(), timeout=5)

    assert excinfo.value.code == TransportErrorCode.IDENTITY_MISMATCH
    assert len(_FakeTCPInterface.instances) == 1
    assert _FakeTCPInterface.instances[0].closed is True
    assert transport.get_connection_info().state == ConnectionState.ERROR
    assert transport.is_connected() is False


def test_failed_connect_rolls_back_to_last_known_good_host(monkeypatch):
    """Same rollback discipline as BLETransport - a failed connect() must
    not leave self._host/_port pointing at the address that just failed,
    or a later bare reconnect() would retry the bad one forever."""
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor("192.168.2.34:4403"), timeout=5)

    _patch_create_connection(monkeypatch, exception=ConnectionRefusedError("refused"))
    with pytest.raises(TransportError):
        transport.connect(_descriptor("10.0.0.99:4403"), force=True, timeout=5)

    info = transport.get_connection_info()
    assert info.descriptor.address == "192.168.2.34:4403"
    assert info.state == ConnectionState.ERROR


# ---------------------------------------------------------------------------
# remote_disconnect
# ---------------------------------------------------------------------------

def test_send_failure_on_dead_socket_reports_remote_disconnect_and_degrades(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]
    interface.sendText = lambda **kwargs: (_ for _ in ()).throw(ConnectionResetError("reset by peer"))

    result = transport.send_text(OutgoingMessage(text="hi", destination_id="^all"), timeout=5)

    assert result.accepted is False
    assert result.error.code == TransportErrorCode.REMOTE_DISCONNECT
    assert transport.internal_state == "degraded"
    assert transport.is_connected() is False


def test_send_messages_stops_retrying_after_first_remote_disconnect(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]

    calls = {"count": 0}

    def _dying_send_text(**kwargs):
        calls["count"] += 1
        raise BrokenPipeError("broken pipe")

    interface.sendText = _dying_send_text

    messages = [
        OutgoingMessage(text="one", destination_id="^all"),
        OutgoingMessage(text="two", destination_id="^all"),
        OutgoingMessage(text="three", destination_id="^all"),
    ]
    results = transport.send_messages(messages, timeout=5)

    assert all(r.accepted is False for r in results)
    assert all(r.error.code == TransportErrorCode.REMOTE_DISCONNECT for r in results)
    # Only the first message actually attempted a real send - the rest
    # were short-circuited against a socket already known to be dead.
    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# describe_endpoint()
# ---------------------------------------------------------------------------

def test_describe_endpoint_shape():
    transport = TCPTransport(host="192.168.2.34", port=4403)

    assert transport.describe_endpoint() == {
        "transport": "tcp",
        "endpoint": {"host": "192.168.2.34", "port": 4403},
    }


def test_describe_endpoint_uses_configured_port_not_hardcoded():
    transport = TCPTransport(host="192.168.2.34", port=5555)

    assert transport.describe_endpoint()["endpoint"]["port"] == 5555


# ---------------------------------------------------------------------------
# Disconnect / reconnect
# ---------------------------------------------------------------------------

def test_disconnect_closes_interface_and_resets_state(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]

    transport.disconnect(timeout=5)

    assert interface.closed is True
    assert transport.is_connected() is False
    assert transport.get_connection_info().state == ConnectionState.DISCONNECTED
    assert transport.internal_state == "disconnected"


def test_disconnect_hard_closes_the_socket_when_the_reader_thread_survives_close(monkeypatch):
    """Adapter-watchdog follow-up (live-caught on pixel-111, T-Beam
    firmware 2.7.15.567b8ea): the real meshtastic library's own
    TCPInterface.close() only waits a short, hardcoded interval for its
    background reader thread to exit, and that depends on the remote
    radio reacting to a half-close promptly - a radio that doesn't
    leaves the thread blocked in a blocking recv() forever, silently
    keeping the raw socket (and the radio's single-TCP-client slot)
    held even though close() itself already returned. Reproduced live
    as a clean alternating pattern: every successful connect-then-
    disconnect cycle left the NEXT connect attempt failing, every other
    one succeeding. _detach_and_close_async() must notice the reader
    thread is still alive after the library's own close() attempt and
    force a hard close of the raw socket itself."""
    _patch_create_connection(monkeypatch)
    _FakeTCPInterface.rx_thread_survives_close = True
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]

    transport.disconnect(timeout=5)

    assert interface.closed is True  # the library's own close() was still tried first
    assert interface.socket.closed is True, (
        "the raw socket must be force-closed when the reader thread survives the library's own close()"
    )


def test_disconnect_does_not_force_close_the_socket_when_the_reader_thread_exits_cleanly(monkeypatch):
    """Negative test guarding the fallback's own scope: when the
    library's close() already stopped the reader thread (the common
    case), the extra hard-close must not run - nothing to guard against
    double-closing an already-closed socket cleanly, but this keeps the
    fallback's trigger condition honest and observable."""
    _patch_create_connection(monkeypatch)
    _FakeTCPInterface.rx_thread_survives_close = False
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]

    transport.disconnect(timeout=5)

    assert interface.closed is True
    assert interface.socket.closed is False, "no reason to force-close a socket the reader thread already released"


def test_disconnect_state_is_correct_even_when_close_never_returns(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]

    release_event = threading.Event()
    interface.close = lambda: release_event.wait(timeout=5)

    transport.disconnect(timeout=0.2)  # must NOT raise - close() failures are logged, not propagated

    assert transport.is_connected() is False
    assert transport.get_connection_info().state == ConnectionState.DISCONNECTED
    with pytest.raises(TransportError):
        transport.get_nodes(timeout=1)

    release_event.set()


def test_reconnect_full_handshake_and_identity_revalidation_before_ready(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34", expected_node_id="!756f9960")
    transport.connect(_descriptor(), timeout=5)
    assert len(_FakeTCPInterface.instances) == 1

    info = transport.reconnect(timeout=5)

    assert info.state == ConnectionState.CONNECTED
    assert info.node_id == "!756f9960"
    # A genuinely NEW interface was opened - reconnect() does not just
    # resurrect the old one.
    assert len(_FakeTCPInterface.instances) == 2
    assert _FakeTCPInterface.instances[0].closed is True


def test_reconnect_uses_bounded_jittered_backoff_no_tight_loop(monkeypatch):
    """Verifies the 1s/2s/5s/10s/30s/60s schedule (with jitter) is what
    reconnect() actually sleeps between attempts - patches time.sleep to
    capture delays instead of the test itself waiting ~108s."""
    _patch_create_connection(monkeypatch)
    _FakeTCPInterface.construct_exception = ConnectionRefusedError("refused")

    sleeps = []
    monkeypatch.setattr(
        "adapters.meshtastic.tcp_transport.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    transport = TCPTransport(host="192.168.2.34")

    with pytest.raises(TransportError) as excinfo:
        transport.reconnect(timeout=5)

    assert excinfo.value.code == TransportErrorCode.CONNECT_REFUSED
    assert transport.get_connection_info().state == ConnectionState.ERROR

    # 6 total attempts (per _RECONNECT_DELAYS_S), 5 sleeps between them -
    # never a trailing sleep after the final, already-failed attempt.
    expected_bases = (1.0, 2.0, 5.0, 10.0, 30.0)
    assert len(sleeps) == len(expected_bases)
    for actual, base in zip(sleeps, expected_bases):
        # +/-15% jitter, floored at 0.5s - "no tight loop".
        assert 0.5 <= actual <= base * 1.15 + 1e-9
        assert actual >= max(0.5, base * 0.85 - 1e-9)


def test_reconnect_succeeding_before_exhausting_the_schedule_stops_early(monkeypatch):
    _patch_create_connection(monkeypatch)

    attempts = {"count": 0}
    real_init = _FakeTCPInterface.__init__

    def _fail_twice_then_succeed(self, hostname, portNumber=DEFAULT_TCP_PORT, connectNow=True):
        attempts["count"] += 1
        if attempts["count"] <= 2:
            raise ConnectionRefusedError("refused")
        real_init(self, hostname, portNumber=portNumber, connectNow=connectNow)

    monkeypatch.setattr(_FakeTCPInterface, "__init__", _fail_twice_then_succeed)
    monkeypatch.setattr("adapters.meshtastic.tcp_transport.time.sleep", lambda seconds: None)

    transport = TCPTransport(host="192.168.2.34")

    info = transport.reconnect(timeout=5)

    assert info.state == ConnectionState.CONNECTED
    assert attempts["count"] == 3


# ---------------------------------------------------------------------------
# Concurrency - same lock-discipline regression class as BLETransport's
# equivalent test.
# ---------------------------------------------------------------------------

def test_concurrent_send_and_disconnect_do_not_race_the_interface(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    interface = _FakeTCPInterface.instances[0]

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


# ---------------------------------------------------------------------------
# TCP lifecycle P0 - connect() is idempotent
# ---------------------------------------------------------------------------

def _healthy_reader():
    """The fake reader thread reports alive() == rx_thread_survives_close -
    default False (built for the close()-survival tests above), which the
    idempotent shortcut's health check correctly reads as a DEAD reader.
    These tests want a live one."""
    _FakeTCPInterface.rx_thread_survives_close = True


def _count_probe_sockets(monkeypatch):
    calls = []

    def _fake_create_connection(address, timeout=None):
        calls.append(address)
        return _FakeSocket()

    monkeypatch.setattr(
        "adapters.meshtastic.tcp_transport.socket.create_connection", _fake_create_connection
    )
    return calls


def test_connect_same_endpoint_when_ready_is_idempotent(monkeypatch):
    _healthy_reader()
    probes = _count_probe_sockets(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")

    first = transport.connect(_descriptor(), timeout=5)
    second = transport.connect(_descriptor(), timeout=5)

    assert first.state == second.state == ConnectionState.CONNECTED
    assert len(_FakeTCPInterface.instances) == 1
    assert _FakeTCPInterface.instances[0].closed is False
    assert len(probes) == 1, "no second raw probe socket either - that is a second TCP client too"
    assert transport.internal_state == "ready"
    assert second.connected_since == first.connected_since


def test_repeated_idempotent_connects_never_leak_an_interface(monkeypatch):
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")

    for _ in range(10):
        transport.connect(_descriptor(), timeout=5)

    assert len(_FakeTCPInterface.instances) == 1
    assert [i.closed for i in _FakeTCPInterface.instances] == [False]


def test_state_stays_ready_across_an_idempotent_connect(monkeypatch):
    """The pre-fix bug: force=False flipped state to CONNECTING, so every
    send failed not_connected while a "probe" ran."""
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)

    transport.connect(_descriptor(), timeout=5)

    assert transport.is_connected() is True
    assert transport.internal_state == "ready"


def test_connect_to_a_different_endpoint_closes_the_old_interface_first(monkeypatch):
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor("192.168.2.34:4403"), timeout=5)

    info = transport.connect(_descriptor("192.168.2.99:4403"), timeout=5)

    assert info.state == ConnectionState.CONNECTED
    assert len(_FakeTCPInterface.instances) == 2
    assert _FakeTCPInterface.instances[0].closed is True
    assert _FakeTCPInterface.instances[1].closed is False
    assert transport.describe_endpoint()["endpoint"]["host"] == "192.168.2.99"


def test_failed_connect_to_a_different_endpoint_still_closes_the_old_interface(monkeypatch):
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor("192.168.2.34:4403"), timeout=5)

    _patch_create_connection(monkeypatch, exception=ConnectionRefusedError("refused"))
    with pytest.raises(TransportError) as excinfo:
        transport.connect(_descriptor("192.168.2.99:4403"), timeout=5)

    assert excinfo.value.code == TransportErrorCode.CONNECT_REFUSED
    assert _FakeTCPInterface.instances[0].closed is True, "never left orphaned behind an ERROR state"
    assert transport.internal_state == "error"
    assert transport.describe_endpoint()["endpoint"]["host"] == "192.168.2.34", "rollback preserved"


def test_ready_with_a_dead_reader_thread_is_not_treated_as_a_live_session(monkeypatch):
    """A radio that rebooted / a "Connection reset by peer" leaves the
    reader thread dead while _state still says READY - the shortcut must
    not return that stale session."""
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    _FakeTCPInterface.instances[0]._rxThread._alive = False

    transport.connect(_descriptor(), timeout=5)

    assert len(_FakeTCPInterface.instances) == 2
    assert _FakeTCPInterface.instances[0].closed is True


def test_ready_with_a_cleared_isconnected_event_is_not_treated_as_a_live_session(monkeypatch):
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    _FakeTCPInterface.instances[0].isConnected = threading.Event()  # never set -> not connected

    transport.connect(_descriptor(), timeout=5)

    assert len(_FakeTCPInterface.instances) == 2


def test_degraded_session_is_replaced_not_reused(monkeypatch):
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)
    transport._state = "degraded"  # what a failed send on a dead socket leaves behind

    transport.connect(_descriptor(), timeout=5)

    assert len(_FakeTCPInterface.instances) == 2
    assert _FakeTCPInterface.instances[0].closed is True
    assert transport.internal_state == "ready"


def test_force_true_still_reconnects_a_healthy_session(monkeypatch):
    """reconnect() depends on this - the idempotent shortcut must not
    swallow an explicit force."""
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport.connect(_descriptor(), timeout=5)

    transport.connect(_descriptor(), force=True, timeout=5)

    assert len(_FakeTCPInterface.instances) == 2
    assert _FakeTCPInterface.instances[0].closed is True


def test_concurrent_connects_open_exactly_one_interface(monkeypatch):
    _healthy_reader()
    _patch_create_connection(monkeypatch)
    _FakeTCPInterface.construct_delay_s = 0.3
    transport = TCPTransport(host="192.168.2.34")
    results = []

    def _run():
        results.append(transport.connect(_descriptor(), timeout=10))

    threads = [threading.Thread(target=_run) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert [r.state for r in results] == [ConnectionState.CONNECTED] * 3
    assert len(_FakeTCPInterface.instances) == 1


def test_connect_reports_busy_instead_of_hanging_when_another_connect_never_finishes(monkeypatch):
    _patch_create_connection(monkeypatch)
    transport = TCPTransport(host="192.168.2.34")
    transport._connect_lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(TransportError) as excinfo:
            transport.connect(_descriptor(), timeout=0.1)
        assert excinfo.value.code == TransportErrorCode.BUSY
        assert time.monotonic() - started < 5
    finally:
        transport._connect_lock.release()
    assert len(_FakeTCPInterface.instances) == 0
