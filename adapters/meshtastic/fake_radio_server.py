"""fake_radio_server — a minimal, in-process fake Meshtastic TCP radio
peer, for integration-testing adapters/meshtastic/tcp_transport.py
against the REAL meshtastic.tcp_interface.TCPInterface end to end (a real
socket, the real wire framing, real protobuf messages) without any
physical hardware.

TEST-ONLY. Never imported by production code (server.py, api/*.py, or
tcp_transport.py itself) - this module exists purely so
tests/test_tcp_transport_integration.py can construct real ToRadio/
FromRadio protobuf messages, which is exactly why it MUST live under
adapters/meshtastic/ rather than under tests/: the CI "GPLv3
license-boundary check" (.github/workflows/ci.yml) statically greps every
tracked .py file OUTSIDE adapters/meshtastic/ for lines starting with
`import meshtastic`/`from meshtastic` and fails the build on a match -
anchored per-line, so even a function-local lazy import inside a test
file under tests/ would still trip it, regardless of whether the
`meshtastic` package happens to be installed in that job's environment.
There is no way to satisfy that gate from a file under tests/; the only
option is keeping every `meshtastic`-importing line inside this
directory, exactly as serial_transport.py/ble_transport.py/ipc_server.py
already do for production code. This file follows the same rule for
test-support code.

Implements exactly enough of the real wire protocol (START1/START2 +
big-endian length-prefixed framing - see
meshtastic.stream_interface.StreamInterface) to drive a real
TCPInterface through its actual handshake: parse the client's initial
ToRadio{want_config_id}, then either send back a minimal but complete
FromRadio sequence (my_info, one node_info, one channel, then
config_complete_id - the exact set of fields
meshtastic.mesh_interface.MeshInterface.waitForConfig() polls for:
myInfo, nodes, and the local node's channels) or a deliberately partial
one (my_info only, then silence) to reproduce the CLASS of problem this
feature's regression acceptance test describes - a firmware that accepts
the TCP connection, sends some FromRadio traffic, and never reaches
config_complete. This is a synthetic reproduction of that failure shape,
not a byte-for-byte capture of any specific firmware's actual output.
"""
from __future__ import annotations

import contextlib
import socket
import threading
from typing import Optional

from meshtastic import channel_pb2, mesh_pb2

START1 = 0x94
START2 = 0xC3
HEADER_LEN = 4


def frame(message) -> bytes:
    """Wraps a protobuf message in the real Meshtastic stream framing -
    mirrors meshtastic.stream_interface.StreamInterface._sendToRadioImpl()
    exactly (same START1/START2 header, same 2-byte big-endian length)."""
    payload = message.SerializeToString()
    length = len(payload)
    header = bytes([START1, START2, (length >> 8) & 0xFF, length & 0xFF])
    return header + payload


def read_to_radio_frame(conn: socket.socket) -> Optional[bytes]:
    """Reads one framed message from `conn`, returning its raw (still
    serialized) payload bytes. Mirrors the client's own reader state
    machine (scan for START1, then START2, then a 2-byte length, then
    that many bytes - see StreamInterface.__reader()) closely enough to
    correctly skip the real client's 32-byte 0xC3 wake-up preamble
    (TCPInterface.connect()) without needing to special-case it. Returns
    None on a closed/empty connection (e.g. a peer that connected and
    disconnected without ever sending a framed message - see
    FakeMeshtasticTcpServer's own handling of TCPTransport's raw
    pre-flight reachability probe, which does exactly this)."""
    buf = bytearray()
    length: Optional[int] = None
    while True:
        chunk = conn.recv(1)
        if not chunk:
            return None
        b = chunk[0]
        if len(buf) == 0:
            if b == START1:
                buf.append(b)
            # else: ignore - matches the real reader treating a stray
            # non-START1 byte (e.g. a wake-up preamble byte) as a log byte.
        elif len(buf) == 1:
            if b == START2:
                buf.append(b)
            else:
                buf = bytearray([b]) if b == START1 else bytearray()
        else:
            buf.append(b)
            if len(buf) == HEADER_LEN:
                length = (buf[2] << 8) | buf[3]
            if length is not None and len(buf) == HEADER_LEN + length:
                return bytes(buf[HEADER_LEN:])


class FakeMeshtasticTcpServer:
    """Listens on an ephemeral localhost port and drives the first REAL
    connection it receives (one that actually sends a framed ToRadio
    message) through either a complete or a deliberately-stalled
    handshake. Tolerates - and discards - a connection that opens and
    closes without sending anything, which is exactly what
    TCPTransport's own raw pre-flight probe (_probe_tcp_reachable()) does
    before the real TCPInterface connection is ever attempted; without
    this tolerance that probe would consume the one connection this
    server is willing to serve."""

    def __init__(self, *, complete_handshake: bool, node_num: int = 0x756F9960):
        self._complete_handshake = complete_handshake
        self._node_num = node_num
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(2)
        self.port = self._listener.getsockname()[1]
        self.accepted_want_config_id: Optional[int] = None
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True, name="fake-meshtastic-tcp-server")
        self._thread.start()

    def _serve(self) -> None:
        self._listener.settimeout(10.0)
        while not self._stop_requested.is_set():
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return

            conn.settimeout(10.0)
            try:
                payload = read_to_radio_frame(conn)
            except Exception:
                with contextlib.suppress(Exception):
                    conn.close()
                continue

            if payload is None:
                # Connected and closed without ever sending a framed
                # ToRadio message - not the real client, discard and keep
                # listening for the next (real) connection.
                with contextlib.suppress(Exception):
                    conn.close()
                continue

            try:
                self._drive_handshake(conn, payload)
            finally:
                with contextlib.suppress(Exception):
                    conn.close()
            return  # served the one real connection - done

    def _drive_handshake(self, conn: socket.socket, payload: bytes) -> None:
        to_radio = mesh_pb2.ToRadio()
        to_radio.ParseFromString(payload)
        self.accepted_want_config_id = to_radio.want_config_id

        my_info = mesh_pb2.FromRadio()
        my_info.my_info.my_node_num = self._node_num
        conn.sendall(frame(my_info))

        if self._complete_handshake:
            node_info = mesh_pb2.FromRadio()
            node_info.node_info.num = self._node_num
            node_info.node_info.user.id = f"!{self._node_num:08x}"
            node_info.node_info.user.long_name = "Fake Test Node"
            node_info.node_info.user.short_name = "FTN"
            conn.sendall(frame(node_info))

            channel = mesh_pb2.FromRadio()
            channel.channel.index = 0
            channel.channel.role = channel_pb2.Channel.Role.PRIMARY
            channel.channel.settings.name = "LongFast"
            conn.sendall(frame(channel))

            complete = mesh_pb2.FromRadio()
            complete.config_complete_id = self.accepted_want_config_id
            conn.sendall(frame(complete))
        # else: deliberately silent from here on - "partial FromRadio
        # traffic, config never completes", the exact regression shape
        # this server exists to reproduce.

        # Stay connected (don't close our end) until told to stop - a
        # config-never-completes firmware doesn't drop the TCP link
        # either, it just never finishes the protocol handshake.
        while not self._stop_requested.wait(timeout=0.1):
            pass

    def shutdown(self) -> None:
        self._stop_requested.set()
        with contextlib.suppress(Exception):
            self._listener.close()
        self._thread.join(timeout=5)


def shrink_internal_handshake_timeout(monkeypatch, seconds: float) -> None:
    """Patches meshtastic.mesh_interface.MeshInterface._waitConnected's
    own internal default timeout (hardcoded to 30.0s - NOT the
    interface's own `timeout` constructor parameter; see
    meshtastic.stream_interface.StreamInterface.connect(), which calls
    self._waitConnected() with no arguments at all) down to `seconds`.

    Only relevant for a deliberately-never-completes-the-handshake test:
    TCPTransport's own external timeout (TimeoutEnforced, tier 1 of the
    RadioTransport ABC's documented timeout contract) already releases
    the CALLER promptly regardless of this patch - that's the whole
    point of the tier-1 guarantee, and what the integration test actually
    asserts on. But tier 1 only releases the caller; the underlying
    library call keeps running, abandoned, on its own background thread
    until ITS OWN internal wait gives up (tier 2 is not guaranteed
    outside the adapter-subprocess model - see the ABC's own docstring).
    Left at the real 30s default, that abandoned thread would keep
    running for a real 30 seconds after every regression-scenario test
    case, slowing the suite and leaving background threads alive for
    longer than necessary. Patching this down only shortens how long
    that already-abandoned work takes to finish cleaning itself up -
    never the test's own pass/fail timing.
    """
    import meshtastic.mesh_interface as mesh_interface_module

    original = mesh_interface_module.MeshInterface._waitConnected

    def _patched(self, timeout: float = seconds):
        return original(self, timeout=timeout)

    monkeypatch.setattr(mesh_interface_module.MeshInterface, "_waitConnected", _patched)
