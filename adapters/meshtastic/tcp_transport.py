"""TCPTransport — the RadioTransport implementation over
`meshtastic.tcp_interface.TCPInterface` (the radio's native TCP API,
default port 4403).

Radio TCP Transport, part 1 (the transport itself): implements
RadioTransport exactly like serial_transport.py/ble_transport.py in this
same directory - same method contract, same lazy `meshtastic` import
discipline (imported only inside the one function that actually needs it,
never at module top level - the same pattern the D0 architecture audit
confirmed for both siblings), same TimeoutEnforced tier-1 watchdog, same
"no library types leave this module" rule (every public method here
returns only the neutral models from meshsrv/radio_transport.py).

NOT wired into TransportRouter, profile storage, or the UI yet - that is
part 2's scope, deliberately excluded here. This module is usable and
independently testable on its own (see tests/test_tcp_transport.py), but
nothing in Core constructs or calls it until part 2 lands.

STATE MACHINE (explicit, not implicit - the whole point of this transport
over reusing the ABC's binary DISCONNECTED/CONNECTING/CONNECTED/ERROR
states for something as observably two-staged as a TCP radio link):

    DISCONNECTED -> CONNECTING -> TCP_CONNECTED -> SYNCING -> READY
                                                           \\-> DEGRADED
                        (any stage can instead land in) -> ERROR
                           READY -> RECONNECTING -> ... -> READY | ERROR

TCP_CONNECTED means "a raw TCP socket to (host, port) is open" - nothing
more. It says nothing about whether the Meshtastic protocol handshake
(TCPInterface's own config-request/wait-for-config dance, the same kind
of blocking constructor-time handshake serial_transport.py's connect()
already leans on as its own proof of connectivity) has even started, let
alone finished. SYNCING means that handshake is in progress. Only READY
means the radio is actually usable - that is the one state
is_connected()/the ABC's ConnectionState.CONNECTED are gated on;
TCP_CONNECTED and SYNCING both report ConnectionState.CONNECTING
externally (see _EXTERNAL_STATE_MAP below), exactly so a caller polling
is_connected() during a slow or stuck handshake never sees a false "yes"
just because the socket opened.

This distinction is not cosmetic - it is what makes the regression
acceptance test for this feature (a firmware build that accepts the TCP
connection, sends some FromRadio traffic, and then never reaches
config_complete) reportable at all, as TransportErrorCode.
PROTOCOL_SYNC_TIMEOUT rather than an indefinite hang or an ambiguous
generic timeout. See _finalize_sync_error()/TransportErrorCode's own
docstrings in meshsrv/radio_transport.py for exactly how that
classification happens.

OWNERSHIP MODEL - same as BLETransport, not SerialTransport, and for the
same underlying reason (per that module's own docstring): a TCP radio
link has no competing local resource the way the physical serial port
does (no --listen subprocess, no radio_lock choreography with Core's own
listener), and opening the full protocol handshake is comparatively
expensive - reopening it on every send_*/get_* call would be wasteful and
risks reproducing the exact "stuck reconnect" failure mode this feature
exists to diagnose. So a single `self._interface` is opened once in
connect() and reused by every subsequent call until disconnect()/close().
Any send_*/get_* called while `self._state != _TcpState.READY` raises/
returns TransportError(NOT_CONNECTED) - this transport never tries to
silently auto-reconnect on a caller's behalf.
"""
from __future__ import annotations

import random
import socket
import threading
import time
from typing import Callable, Optional, Sequence

from adapters.meshtastic._timeout_support import TimeoutEnforced
from meshsrv.node_time_sync import try_sync as try_node_time_sync
from meshsrv.radio_transport import (
    ChannelInfo,
    CheckedSendResult,
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    NodeUser,
    OutgoingMessage,
    OutgoingWaypoint,
    RadioTransport,
    SendResult,
    TransportError,
    TransportErrorCode,
    WaypointResult,
)

# The Meshtastic radio's own default TCP port. Defined exactly once here -
# nowhere else in this module (or, per this PR's scope, anywhere else in
# the codebase) hardcodes 4403 as a literal.
DEFAULT_TCP_PORT = 4403

# Ceiling on the raw TCP pre-flight probe phase (_probe_tcp_reachable) -
# not a proportional split of the caller's overall `timeout` budget, since
# a bare TCP connect over LAN/Wi-Fi is cheap and fast compared to the
# Meshtastic protocol handshake that follows it. Capping this small and
# fixed, rather than proportional, leaves the large majority of a
# caller's declared budget for the handshake phase, which is where a
# broken/slow firmware (this feature's own regression acceptance test)
# actually burns time.
_TCP_PROBE_TIMEOUT_S = 10.0

# Reconnect backoff schedule, per this feature's own spec: 1s, 2s, 5s,
# 10s, 30s, capped at 60s - not exponential/unbounded, not BLETransport's
# simpler fixed-3-attempt "naive reconnect" (plan section 5.5 predates
# this feature). Six attempts total; the sixth and any further retry a
# caller triggers on its own reuses the 60s cap rather than growing past
# it.
_RECONNECT_DELAYS_S = (1.0, 2.0, 5.0, 10.0, 30.0, 60.0)

# +/- jitter applied to each backoff delay so multiple reconnecting
# instances (or repeated reconnect() calls) don't converge into a tight,
# synchronized retry loop against the same radio - "with jitter... no
# tight loop" per spec. A ratio, not a fixed number of seconds, so it
# scales sensibly across the whole 1s-60s range.
_RECONNECT_JITTER_RATIO = 0.15

# Never let jitter (or a pathologically small base delay) collapse a
# reconnect attempt into a near-zero-delay retry - this is the actual
# "no tight loop" enforcement, not just documentation.
_RECONNECT_MIN_DELAY_S = 0.5


class _TcpState:
    """Internal, fine-grained connection state - deliberately NOT the
    ABC's ConnectionState (see this module's own docstring for why the
    two are kept separate). Plain string constants rather than an Enum
    subclass so equality/repr stay simple in log lines and test asserts;
    nothing outside this module is meant to depend on these values, so
    there is no compatibility reason to make them a public enum."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    TCP_CONNECTED = "tcp_connected"
    SYNCING = "syncing"
    READY = "ready"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    ERROR = "error"


# How each internal _TcpState maps onto the ABC's own, coarser
# ConnectionState - the one place that mapping is defined, so
# get_connection_info()/is_connected() can never disagree with each
# other about it.
_EXTERNAL_STATE_MAP = {
    _TcpState.DISCONNECTED: ConnectionState.DISCONNECTED,
    _TcpState.CONNECTING: ConnectionState.CONNECTING,
    _TcpState.TCP_CONNECTED: ConnectionState.CONNECTING,
    _TcpState.SYNCING: ConnectionState.CONNECTING,
    _TcpState.READY: ConnectionState.CONNECTED,
    _TcpState.DEGRADED: ConnectionState.ERROR,
    _TcpState.RECONNECTING: ConnectionState.CONNECTING,
    _TcpState.ERROR: ConnectionState.ERROR,
}


def _parse_host_port(address: str, default_port: int) -> tuple[str, int]:
    """Parses "host:port" (or a bracketed "[host]:port" for a literal
    IPv6 address) into (host, port), defaulting to `default_port` when no
    port is present. Matches the RadioTransport ABC's own documented
    address shape for TCP ("host:port for tcp" - see
    ConnectionDescriptor's docstring in meshsrv/radio_transport.py)."""
    address = (address or "").strip()
    if not address:
        return "", default_port

    if address.startswith("["):
        closing = address.find("]")
        if closing != -1:
            host = address[1:closing]
            rest = address[closing + 1:]
            if rest.startswith(":") and rest[1:].isdigit():
                return host, int(rest[1:])
            return host, default_port
        # Malformed ("[" with no closing "]") - fall through and treat
        # the whole string as the host rather than raising here; connect()
        # will fail cleanly against whatever garbage hostname this
        # produces instead of this parser itself needing its own error path.
        return address, default_port

    if address.count(":") == 1:
        host, _, port_text = address.partition(":")
        if port_text.isdigit():
            return host, int(port_text)
        return address, default_port

    # No unambiguous port separator (a bare hostname/IPv4 address, or a
    # literal IPv6 address with multiple colons and no brackets) - treat
    # the whole string as the host.
    return address, default_port


class TCPTransport(TimeoutEnforced, RadioTransport):
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_TCP_PORT,
        expected_node_id: Optional[str] = None,
        on_log: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        TimeoutEnforced.__init__(self, thread_name_prefix="tcp-transport-watchdog")
        self._host = host
        self._port = int(port) if port else DEFAULT_TCP_PORT
        self._label = ""
        self._expected_node_id = expected_node_id
        self._on_log = on_log or (lambda msg, level="INFO": None)

        # Guards every mutation of _interface/_state and every read+use
        # of _interface - same discipline, and the same reason, as
        # BLETransport's own self._lock (see that module's docstring):
        # without it, a concurrent disconnect()/reconnect() could null
        # out self._interface while a send_*/get_* call already in
        # flight is still using it. NOT reentrant - internal helpers that
        # touch self._state/_interface must already hold this lock, never
        # acquire it themselves.
        self._lock = threading.Lock()

        self._interface = None
        self._state = _TcpState.DISCONNECTED
        self._connected_since: Optional[float] = None
        self._last_error: Optional[TransportError] = None
        self._node_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @property
    def internal_state(self) -> str:
        """The fine-grained _TcpState value, NOT part of the RadioTransport
        ABC - exposed read-only for diagnostics/logging and so tests can
        assert on the exact stage a connect() attempt reached, rather than
        only the coarser externally-visible ConnectionState."""
        with self._lock:
            return self._state

    def describe_endpoint(self) -> dict:
        """Structured connection-descriptor JSON, distinct from the ABC's
        own ConnectionDescriptor(type, address, label) - useful anywhere a
        clearer {"transport": ..., "endpoint": {"host": ..., "port": ...}}
        shape is more useful than a single "host:port" address string
        (diagnostics, log lines, a future part-2 UI). Not part of the
        RadioTransport ABC."""
        return {
            "transport": ConnectionType.TCP.value,
            "endpoint": {"host": self._host, "port": self._port},
        }

    def _require_connected(self) -> None:
        """Caller MUST already hold self._lock - this does not acquire
        it (self._lock is a plain, non-reentrant Lock)."""
        if self._state != _TcpState.READY or self._interface is None:
            raise TransportError(TransportErrorCode.NOT_CONNECTED, "TCPTransport is not connected")

    @staticmethod
    def _local_node_id(interface) -> Optional[str]:
        my_info = getattr(interface, "myInfo", None)
        num = getattr(my_info, "my_node_num", None)
        if num is None:
            return None
        return f"!{int(num):08x}"

    def _detach_and_close_async(self, *, timeout: float) -> None:
        """Detaches self._interface and flips self._state to DISCONNECTED
        synchronously, under self._lock, BEFORE closing the detached
        interface in the background - same ownership-transfer fix as
        BLETransport._detach_and_close_async() (see that module's
        docstring for the live-caught bug this shape avoids): a caller
        checking is_connected()/get_connection_info() right after
        disconnect() returns must never observe a stale READY pointing at
        an interface a now-abandoned thread is still closing."""
        with self._lock:
            interface_to_close = self._interface
            self._interface = None
            self._state = _TcpState.DISCONNECTED
            self._connected_since = None

        def _do_close() -> None:
            if interface_to_close is not None:
                try:
                    interface_to_close.close()
                except Exception:
                    pass

        try:
            self._call_with_timeout(_do_close, timeout=timeout, what="close interface")
        except TransportError as error:
            # Own state is already correct (detached above) regardless -
            # this is purely "how long did the background close() take",
            # not a correctness signal, so it's logged, not re-raised.
            self._on_log(f"TCP interface close() did not finish within {timeout}s: {error}", "WARNING")

    def _probe_tcp_reachable(self, timeout: float) -> None:
        """Prove a raw TCP socket to (self._host, self._port) is
        reachable, entirely independent of the Meshtastic protocol layer
        - this is what lets connect() report dns_error/connect_refused/
        connect_timeout distinctly instead of folding every possible
        failure into whatever generic exception
        meshtastic.tcp_interface.TCPInterface's own combined
        connect+handshake constructor happens to raise. Uses the stdlib
        `socket` module directly (not `meshtastic`, so this probe - and
        the diagnostic split it enables - needs no GPLv3 import and is
        testable with nothing more than a monkeypatched
        socket.create_connection)."""
        try:
            probe_socket = socket.create_connection((self._host, self._port), timeout=timeout)
        except socket.gaierror as error:
            raise TransportError(
                TransportErrorCode.DNS_ERROR, f"could not resolve {self._host!r}: {error}"
            ) from error
        except ConnectionRefusedError as error:
            raise TransportError(
                TransportErrorCode.CONNECT_REFUSED,
                f"{self._host}:{self._port} refused the connection: {error}",
            ) from error
        except TimeoutError as error:
            # socket.timeout is TimeoutError itself since Python 3.10 -
            # this project targets 3.14 (see _timeout_support.py), but
            # catching the modern name only, not both, matches how the
            # rest of this module is written for that same target.
            raise TransportError(
                TransportErrorCode.CONNECT_TIMEOUT,
                f"connecting to {self._host}:{self._port} exceeded {timeout}s: {error}",
            ) from error
        except OSError as error:
            # Anything else at the socket layer (network unreachable, host
            # unreachable, and similar) - not one of the three specifically
            # named cases above, but still clearly a raw-connect failure,
            # not a protocol one.
            raise TransportError(
                TransportErrorCode.CONNECT_FAILED,
                f"could not open a TCP connection to {self._host}:{self._port}: {error}",
            ) from error
        else:
            try:
                probe_socket.close()
            except Exception:
                pass

    def _classify_socket_error(self, exc: BaseException) -> Optional[TransportError]:
        """Shared classifier for a raw socket-layer exception, used both
        by _probe_tcp_reachable() (via its own explicit except clauses)
        and defensively around _open_interface() - a race is possible
        where the probe succeeds but the library's own internal connect
        fails a moment later (remote closed right after the probe).
        Returns None when `exc` is not a socket-layer error at all, so
        the caller knows to fall through to protocol-layer classification
        instead."""
        if isinstance(exc, socket.gaierror):
            return TransportError(TransportErrorCode.DNS_ERROR, f"could not resolve {self._host!r}: {exc}")
        if isinstance(exc, ConnectionRefusedError):
            return TransportError(
                TransportErrorCode.CONNECT_REFUSED, f"{self._host}:{self._port} refused the connection: {exc}"
            )
        if isinstance(exc, TimeoutError):
            return TransportError(
                TransportErrorCode.CONNECT_TIMEOUT, f"connecting to {self._host}:{self._port} timed out: {exc}"
            )
        if isinstance(exc, OSError):
            return TransportError(
                TransportErrorCode.CONNECT_FAILED, f"TCP connect to {self._host}:{self._port} failed: {exc}"
            )
        return None

    def _classify_sync_failure(self, exc: Exception) -> TransportError:
        """Turns whatever _open_interface() raised into a properly-coded
        TransportError BEFORE _call_with_timeout wraps it - that mixin
        only ever wraps a non-TransportError as TransportErrorCode.UNKNOWN
        (see adapters/meshtastic/_timeout_support.py), so any finer
        classification has to happen here, on the raising side."""
        socket_error = self._classify_socket_error(exc)
        if socket_error is not None:
            return socket_error
        # Not a socket-layer error at all - the raw TCP connection is
        # already proven good (this only ever runs after
        # _probe_tcp_reachable() succeeded and this transport's own state
        # already flipped to SYNCING), so whatever this is came from
        # inside the Meshtastic protocol layer itself, and it raised
        # immediately rather than hanging - TCP_CONNECTED (see that
        # code's own docstring in meshsrv/radio_transport.py for exactly
        # why this isn't PROTOCOL_SYNC_TIMEOUT).
        return TransportError(
            TransportErrorCode.TCP_CONNECTED,
            f"Meshtastic protocol handshake with {self._host}:{self._port} failed immediately: {exc}",
        )

    def _finalize_sync_error(self, error: TransportError) -> TransportError:
        """Reclassifies _call_with_timeout's own generic
        TransportError(TIMEOUT) into PROTOCOL_SYNC_TIMEOUT specifically
        when this transport's internal state was already SYNCING at the
        moment the watchdog fired - i.e. the handshake was genuinely in
        progress and never returned, not merely "some call somewhere
        exceeded its budget". Any other error (already classified by
        _classify_sync_failure, propagated through _call_with_timeout
        unchanged since it's already a TransportError) passes through as-is."""
        if error.code != TransportErrorCode.TIMEOUT:
            return error
        with self._lock:
            state = self._state
        if state != _TcpState.SYNCING:
            return error
        return TransportError(
            TransportErrorCode.PROTOCOL_SYNC_TIMEOUT,
            f"Meshtastic protocol handshake with {self._host}:{self._port} did not complete: {error.message}",
        )

    def _classify_remote_failure_locked(self, exc: Exception) -> TransportError:
        """Caller MUST already hold self._lock - every call site is
        already inside a `with self._lock:` block guarding the
        send_*() interface call that raised `exc` (self._lock is a
        plain, non-reentrant Lock, so this must NOT acquire it itself).

        A previously READY interface's own socket died mid-operation -
        see TransportErrorCode.REMOTE_DISCONNECT's own docstring in
        meshsrv/radio_transport.py. Flips this transport's state to
        DEGRADED, not ERROR: a fresh connect()/reconnect() attempt against
        the same radio is still meaningful (the radio may still be up,
        this specific TCP link just died), unlike a connect()-time failure
        where nothing ever worked in the first place."""
        if isinstance(exc, OSError):
            error = TransportError(
                TransportErrorCode.REMOTE_DISCONNECT,
                f"TCP connection to {self._host}:{self._port} was lost: {exc}",
            )
        else:
            error = TransportError(TransportErrorCode.UNKNOWN, str(exc))
        self._state = _TcpState.DEGRADED
        self._last_error = error
        return error

    def _open_interface(self):
        """The one lazy `meshtastic` import in this module - see the
        module docstring's lazy-import-discipline note. `connectNow=True`
        (TCPInterface's own default) is what makes this call block for
        the FULL raw-connect + protocol-handshake sequence, the same way
        SerialInterface's constructor already does for SerialTransport -
        see that module's connect() docstring for the equivalent proof-
        of-connectivity reasoning this mirrors for TCP."""
        from meshtastic.tcp_interface import TCPInterface

        return TCPInterface(hostname=self._host, portNumber=self._port, connectNow=True)

    # ------------------------------------------------------------------
    # RadioTransport - connection lifecycle
    # ------------------------------------------------------------------
    def connect(
        self, descriptor: ConnectionDescriptor, *, force: bool = False, timeout: float = 30.0
    ) -> ConnectionInfo:
        """Left at the ABC's own 30.0s default, unlike BLETransport's
        live-measured 90.0s override - there is no equivalent live TCP
        measurement to raise this default from yet (this PR's own
        acceptance criteria are a manual hardware test the author runs,
        not something this code can claim to have already observed). A
        LAN/Wi-Fi TCP connect is expected to be considerably faster than
        a BLE GATT handshake in the first place, so the ABC's default is
        plausibly already generous; revisit with real numbers once the
        acceptance tests against the T-Beam have actually run."""
        if descriptor.type != ConnectionType.TCP:
            raise TransportError(
                TransportErrorCode.UNSUPPORTED, f"TCPTransport cannot connect to {descriptor.type}"
            )

        # Snapshot before any mutation, restored on every failure path
        # below - same rollback discipline as BLETransport.connect() (see
        # that module's own docstring for the live TAP2 finding this
        # guards against): a failed connect() must never leave
        # self._host/_port pointing at the address that just failed, or a
        # later bare reconnect() (which rebuilds its descriptor from
        # these fields, not a fresh caller-supplied one) would retry the
        # bad address forever instead of the last-known-good one.
        previous_host, previous_port, previous_label = self._host, self._port, self._label

        host, port = _parse_host_port(descriptor.address, default_port=self._port or DEFAULT_TCP_PORT)
        self._host = host or self._host
        self._port = port or self._port
        self._label = descriptor.label or self._label

        with self._lock:
            self._state = _TcpState.CONNECTING

        if force:
            self._detach_and_close_async(timeout=timeout)

        probe_timeout = min(timeout, _TCP_PROBE_TIMEOUT_S)
        try:
            self._probe_tcp_reachable(probe_timeout)
        except TransportError as error:
            with self._lock:
                self._state = _TcpState.ERROR
                self._last_error = error
                self._host, self._port, self._label = previous_host, previous_port, previous_label
            raise

        with self._lock:
            self._state = _TcpState.TCP_CONNECTED

        remaining = max(1.0, timeout - probe_timeout)

        def _do_sync():
            with self._lock:
                self._state = _TcpState.SYNCING
            try:
                return self._open_interface()
            except Exception as exc:
                raise self._classify_sync_failure(exc) from exc

        try:
            interface = self._call_with_timeout(_do_sync, timeout=remaining, what="connect() protocol sync")
        except TransportError as error:
            error = self._finalize_sync_error(error)
            with self._lock:
                self._state = _TcpState.ERROR
                self._last_error = error
                self._host, self._port, self._label = previous_host, previous_port, previous_label
            raise error

        node_id = self._local_node_id(interface)

        if self._expected_node_id and node_id != self._expected_node_id:
            # IDENTITY MISMATCH TEARDOWN - never leave a live connection to
            # the wrong radio dangling, same spirit as BLETransport's
            # equivalent guard and the ABC's disconnect()/close()
            # completion guarantee.
            try:
                interface.close()
            except Exception:
                pass
            error = TransportError(
                TransportErrorCode.IDENTITY_MISMATCH,
                f"Connected TCP radio {node_id!r} does not match expected {self._expected_node_id!r}",
            )
            with self._lock:
                self._state = _TcpState.ERROR
                self._last_error = error
                self._host, self._port, self._label = previous_host, previous_port, previous_label
            raise error

        with self._lock:
            self._interface = interface
            self._node_id = node_id
            self._state = _TcpState.READY
            self._connected_since = time.time()
            self._last_error = None
        return self.get_connection_info()

    def disconnect(self, *, timeout: float = 15.0) -> None:
        self._detach_and_close_async(timeout=timeout)

    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        """Bounded backoff per this feature's own spec (see
        _RECONNECT_DELAYS_S) - full handshake + identity re-validation on
        every attempt, via connect(force=True), never a lighter-weight
        "just reopen the socket" path: a reconnect that skips identity
        re-validation could silently start talking to a different radio
        than the one this transport was originally bound to."""
        with self._lock:
            self._state = _TcpState.RECONNECTING
        self.disconnect(timeout=min(timeout, 15.0))

        last_error: Optional[TransportError] = None
        attempts = len(_RECONNECT_DELAYS_S)
        for attempt, base_delay in enumerate(_RECONNECT_DELAYS_S, start=1):
            try:
                return self.connect(
                    ConnectionDescriptor(
                        type=ConnectionType.TCP, address=f"{self._host}:{self._port}", label=self._label
                    ),
                    force=True,
                    timeout=timeout,
                )
            except TransportError as error:
                last_error = error
                self._on_log(f"TCP reconnect attempt {attempt}/{attempts} failed: {error}", "WARNING")
                if attempt < attempts:
                    time.sleep(self._jittered_delay(base_delay))

        with self._lock:
            self._state = _TcpState.ERROR
            self._last_error = last_error
        raise last_error or TransportError(TransportErrorCode.CONNECT_FAILED, "reconnect() exhausted all attempts")

    @staticmethod
    def _jittered_delay(base_delay: float) -> float:
        jitter = base_delay * random.uniform(-_RECONNECT_JITTER_RATIO, _RECONNECT_JITTER_RATIO)
        return max(_RECONNECT_MIN_DELAY_S, base_delay + jitter)

    def is_connected(self) -> bool:
        with self._lock:
            return self._state == _TcpState.READY and self._interface is not None

    def get_connection_info(self) -> ConnectionInfo:
        with self._lock:
            return ConnectionInfo(
                state=_EXTERNAL_STATE_MAP[self._state],
                descriptor=ConnectionDescriptor(
                    type=ConnectionType.TCP, address=f"{self._host}:{self._port}", label=self._label
                ),
                node_id=self._node_id,
                connected_since=self._connected_since,
                last_error=self._last_error,
            )

    def close(self) -> None:
        self.disconnect(timeout=15.0)
        self._shutdown_executor()

    # ------------------------------------------------------------------
    # RadioTransport - sending. Persistent self._interface (see module
    # docstring's OWNERSHIP MODEL) - not reopened per call.
    # ------------------------------------------------------------------
    def send_text(self, message: OutgoingMessage, *, timeout: float = 15.0) -> SendResult:
        results = self.send_messages([message], timeout=timeout)
        return results[0]

    def send_text_checked(self, message: OutgoingMessage, *, timeout: float = 15.0) -> CheckedSendResult:
        """Resolve `message.channel_index` against the live radio AND
        send, in ONE session under this transport's already-open
        persistent interface - TCP has no per-call open/close (see the
        module docstring's OWNERSHIP MODEL). Raises
        TransportError(UNSUPPORTED) via _channel_name_for_index when the
        requested channel is absent/DISABLED, matching both siblings'
        fail-closed behavior."""

        def _do_send_checked():
            with self._lock:
                self._require_connected()
                channel_name = self._channel_name_for_index(self._interface, message.channel_index)
                try:
                    sent = self._interface.sendText(
                        text=message.text,
                        destinationId=message.destination_id,
                        wantAck=message.want_ack,
                        channelIndex=message.channel_index,
                        replyId=message.reply_id,
                    )
                except Exception as exc:
                    raise self._classify_remote_failure_locked(exc) from exc
                packet_id = getattr(sent, "id", None)
                return CheckedSendResult(
                    result=SendResult(accepted=True, packet_id=int(packet_id) if packet_id is not None else None),
                    channel_name=channel_name,
                )

        return self._call_with_timeout(_do_send_checked, timeout=timeout, what="send_text_checked()")

    def send_packet(
        self,
        payload: bytes,
        destination_id: str,
        *,
        port_num: int,
        want_ack: bool = False,
        timeout: float = 15.0,
    ) -> SendResult:
        def _do_send():
            with self._lock:
                self._require_connected()
                try:
                    sent = self._interface.sendData(
                        payload,
                        destinationId=destination_id,
                        portNum=port_num,
                        wantAck=want_ack,
                    )
                except Exception as exc:
                    raise self._classify_remote_failure_locked(exc) from exc
                packet_id = getattr(sent, "id", None)
                return SendResult(accepted=True, packet_id=int(packet_id) if packet_id is not None else None)

        try:
            return self._call_with_timeout(_do_send, timeout=timeout, what="send_packet()")
        except TransportError as error:
            return SendResult(accepted=False, error=error)

    def send_messages(
        self, messages: Sequence[OutgoingMessage], *, timeout: float = 30.0
    ) -> list[SendResult]:
        """Trivial loop over the already-open self._interface - "one
        connection for the whole batch" is automatically true because
        it's one connection for everything until disconnect() (see module
        docstring's OWNERSHIP MODEL). Stops early once the underlying
        socket is confirmed dead (a REMOTE_DISCONNECT-classified failure)
        instead of retrying every remaining message against a connection
        already known to be gone - every message after the first failure
        of that kind gets the same error without a further attempt."""

        def _do_send_all():
            with self._lock:
                self._require_connected()
                results: list[SendResult] = []
                remote_gone: Optional[TransportError] = None
                for message in messages:
                    if remote_gone is not None:
                        results.append(SendResult(accepted=False, error=remote_gone))
                        continue
                    try:
                        sent = self._interface.sendText(
                            text=message.text,
                            destinationId=message.destination_id,
                            wantAck=message.want_ack,
                            channelIndex=message.channel_index,
                            replyId=message.reply_id,
                        )
                        packet_id = getattr(sent, "id", None)
                        results.append(
                            SendResult(accepted=True, packet_id=int(packet_id) if packet_id is not None else None)
                        )
                    except Exception as e:
                        error = self._classify_remote_failure_locked(e)
                        if error.code == TransportErrorCode.REMOTE_DISCONNECT:
                            remote_gone = error
                        results.append(SendResult(accepted=False, error=error))
                return results

        try:
            return self._call_with_timeout(_do_send_all, timeout=timeout, what="send_messages()")
        except TransportError as error:
            return [SendResult(accepted=False, error=error) for _ in messages]

    def send_waypoint(self, waypoint: OutgoingWaypoint, *, timeout: float = 15.0) -> WaypointResult:
        import secrets

        def _waypoint_id() -> int:
            return secrets.randbelow(1_000_000_000 - 1) + 1

        def _do_send():
            with self._lock:
                self._require_connected()
                try:
                    waypoint_id = int(waypoint.waypoint_id or _waypoint_id())
                    waypoint_packet = self._interface.sendWaypoint(
                        name=waypoint.name,
                        description=waypoint.description,
                        icon=int(waypoint.icon),
                        expire=int(waypoint.expire_at),
                        waypoint_id=waypoint_id,
                        latitude=float(waypoint.latitude),
                        longitude=float(waypoint.longitude),
                        channelIndex=int(waypoint.channel_index),
                        wantAck=True,
                        wantResponse=False,
                    )

                    notification_packet_id = None
                    if waypoint.post_notification and waypoint.notification_text.strip():
                        notification_packet = self._interface.sendText(
                            text=waypoint.notification_text,
                            destinationId="^all",
                            channelIndex=int(waypoint.channel_index),
                            wantAck=False,
                            wantResponse=False,
                        )
                        notification_packet_id = int(notification_packet.id)
                except Exception as exc:
                    raise self._classify_remote_failure_locked(exc) from exc

                return WaypointResult(
                    waypoint_id=waypoint_id,
                    waypoint_packet_id=int(waypoint_packet.id),
                    notification_packet_id=notification_packet_id,
                )

        return self._call_with_timeout(_do_send, timeout=timeout, what="send_waypoint()")

    # ------------------------------------------------------------------
    # RadioTransport - reads
    # ------------------------------------------------------------------
    def get_nodes(self, *, timeout: float = 15.0) -> list[NodeInfo]:
        def _do_get():
            with self._lock:
                self._require_connected()
                raw_nodes = getattr(self._interface, "nodes", None) or {}
                return [self._to_node_info(node_id, data) for node_id, data in raw_nodes.items()]

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_nodes()")

    def get_local_node(self, *, timeout: float = 15.0) -> NodeInfo:
        def _do_get():
            with self._lock:
                self._require_connected()
                local = getattr(self._interface, "localNode", None)
                node_num = getattr(local, "nodeNum", None)
                raw_nodes = getattr(self._interface, "nodes", None) or {}
                for node_id, data in raw_nodes.items():
                    if data.get("num") == node_num:
                        return self._to_node_info(node_id, data)
                raise TransportError(TransportErrorCode.UNKNOWN, "Local node not found in node list")

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_local_node()")

    def get_channels(self, *, timeout: float = 15.0) -> list[ChannelInfo]:
        def _do_get():
            with self._lock:
                self._require_connected()
                raw_channels = getattr(getattr(self._interface, "localNode", None), "channels", None) or []
                channels = []
                for fallback_index, channel in enumerate(raw_channels):
                    index = getattr(channel, "index", fallback_index)
                    try:
                        index = int(index)
                    except (TypeError, ValueError):
                        index = fallback_index
                    if index < 0 or index > 7:
                        continue
                    settings_obj = getattr(channel, "settings", None)
                    name = getattr(settings_obj, "name", "") if settings_obj is not None else ""
                    role = getattr(channel, "role", None)
                    # Same role-int-to-name mapping as both siblings
                    # (mesh_pb2.Channel.Role: 0=DISABLED, 1=PRIMARY,
                    # 2=SECONDARY).
                    role_name = {0: "DISABLED", 1: "PRIMARY", 2: "SECONDARY"}.get(role, str(role))
                    if role == 0:
                        continue
                    channels.append(ChannelInfo(index=index, name=name, role=role_name))
                return channels

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_channels()")

    @staticmethod
    def _channel_name_for_index(interface, index: int) -> str:
        """Resolve `index` to the connected radio's channel name, raising
        TransportError(UNSUPPORTED) when the channel is absent or
        DISABLED - fail-closed, matching both siblings' behavior. Returns
        only the channel *name* (never a PSK/secret)."""
        raw_channels = getattr(getattr(interface, "localNode", None), "channels", None) or []
        for fallback_index, channel in enumerate(raw_channels):
            idx = getattr(channel, "index", fallback_index)
            try:
                idx = int(idx)
            except (TypeError, ValueError):
                idx = fallback_index
            if idx < 0 or idx > 7:
                continue
            role = getattr(channel, "role", None)
            if role == 0:
                continue
            if idx == index:
                settings_obj = getattr(channel, "settings", None)
                return getattr(settings_obj, "name", "") if settings_obj is not None else ""
        raise TransportError(
            TransportErrorCode.UNSUPPORTED,
            f"MCA control channel {index} is not available on the connected radio",
        )

    def get_metadata(self, *, timeout: float = 15.0) -> dict:
        """Unlike SerialTransport.get_metadata() (which shells out to a
        second, independent `meshtastic --info` CLI call - TCP has no
        such CLI equivalent to fall back on), this reads the already-open
        self._interface's own `.metadata` field, same approach and same
        rationale as BLETransport.get_metadata(): TCPInterface is, like
        BLEInterface, populated with a `mesh_pb2.DeviceMetadata` during
        connect()'s handshake, and opening a second connection just to
        re-fetch it risks the same "concurrent session" hazard both
        siblings already avoid.

        NAMED GAP, same as both siblings: returns the metadata as a JSON
        *string* (via protobuf's MessageToJson), not a parsed dict with
        individual fields - whoever wires this into Core in part 2 must
        parse it if structured fields are needed."""

        def _do_get():
            with self._lock:
                self._require_connected()
                from google.protobuf.json_format import MessageToJson

                metadata = getattr(self._interface, "metadata", None)
                if metadata is None:
                    return {"metadata_json": ""}
                return {"metadata_json": MessageToJson(metadata)}

        return self._call_with_timeout(_do_get, timeout=timeout, what="get_metadata()")

    @staticmethod
    def _to_node_info(node_id: str, data: dict) -> NodeInfo:
        user_data = data.get("user") or {}
        user = NodeUser(
            id=user_data.get("id", node_id),
            long_name=user_data.get("longName", ""),
            short_name=user_data.get("shortName", ""),
            hw_model=user_data.get("hwModel", ""),
            is_licensed=bool(user_data.get("isLicensed", False)),
        ) if user_data else None
        return NodeInfo(
            node_id=node_id,
            num=data.get("num", 0),
            user=user,
            last_heard=data.get("lastHeard"),
            snr=data.get("snr"),
            rssi=data.get("rssi"),
            hop_count=data.get("hopsAway"),
            is_favorite=bool(data.get("isFavorite", False)),
            device_metrics=data.get("deviceMetrics") or {},
            environment_metrics=data.get("environmentMetrics") or {},
            power_metrics=data.get("powerMetrics") or {},
            position=data.get("position"),
        )

    # ------------------------------------------------------------------
    # RadioTransport - device time
    # ------------------------------------------------------------------
    def set_device_time(self, epoch_seconds: int, *, timeout: float = 15.0) -> bool:
        """Same KNOWN SIGNATURE MISMATCH as both siblings'
        set_device_time() (epoch_seconds accepted per the ABC but not
        passed through - try_sync() derives system time itself and gates
        on is_trusted()/MIN_SYNC_INTERVAL_S) - see
        SerialTransport.set_device_time()'s docstring for the full
        rationale, unchanged here."""

        def _do_sync():
            with self._lock:
                self._require_connected()
                outcome = try_node_time_sync(
                    interface=self._interface,
                    log_fn=lambda msg, level="INFO": self._on_log(msg, level),
                )
                return outcome == "synced"

        return self._call_with_timeout(_do_sync, timeout=timeout, what="set_device_time()")
