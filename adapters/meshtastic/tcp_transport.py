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

import collections
import random
import socket
import threading
import time
from typing import Callable, Optional, Sequence

from adapters.meshtastic._json_safe import json_safe
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
    ReceivedBatch,
    ReceivedNodeInfoEvent,
    ReceivedPositionEvent,
    ReceivedTelemetryEvent,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
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

# reconnect()'s `timeout` is ONE budget for the whole call (initial
# disconnect + every attempt + every backoff sleep), not a per-attempt
# allowance. An attempt (or the sleep before one) is only started if at
# least this much budget is left for it to be meaningful: a connect() with
# a second or two to live can only time out, and a timeout / tcp_connected
# failure makes Core recycle the whole adapter process.
_RECONNECT_MIN_ATTEMPT_S = 5.0


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


# ---------------------------------------------------------------------------
# Inbound (text + waypoints) - see docs/BACKEND_API.md "Received events"
# ---------------------------------------------------------------------------
# The meshtastic library already receives every packet on its reader thread and
# publishes it through pypubsub. Only these two topics are subscribed - not the
# catch-all `meshtastic.receive` - so no NodeInfo/position/telemetry is ingested
# by accident. (For a known protocol the library REPLACES the
# `meshtastic.receive.data.<PORTNUM>` topic with `meshtastic.receive.<name>`,
# so subscribing to the former would receive nothing - see
# verify_receive_topics.py.)
_RECEIVE_TOPIC_TEXT = "meshtastic.receive.text"
_RECEIVE_TOPIC_WAYPOINT = "meshtastic.receive.waypoint"
_RECEIVE_TOPIC_NODEINFO = "meshtastic.receive.user"  # the library's own protocol name is "user", not "nodeinfo"
_RECEIVE_TOPIC_POSITION = "meshtastic.receive.position"
_RECEIVE_TOPIC_TELEMETRY = "meshtastic.receive.telemetry"

# LoRa traffic is slow; 256 is a large margin for a queue Core drains about once
# a second. Bounded, and NOT a silent deque(maxlen=N): overflow drops the OLDEST
# event, is counted, and is reported to the caller in the next batch.
_RECEIVE_QUEUE_CAPACITY = 256

# One WARNING per this many seconds while overflowing, not one per dropped event.
_RECEIVE_OVERFLOW_LOG_INTERVAL_S = 60.0

_BROADCAST_NUM = 0xFFFFFFFF


def _plain_int(value) -> Optional[int]:
    """An int from the library's packet dict, or None. bool is not an int here,
    and anything that is not already a plain int is not coerced."""
    return value if type(value) is int else None


def _plain_int_or_none_if_zero(value) -> Optional[int]:
    """The library omits protobuf zero-defaults from the dict and 0 means
    "unknown" for these fields (id, rxTime, rxRssi, relayNode) - normalise 0 to
    None so a real value and "not reported" cannot be confused."""
    number = _plain_int(value)
    return number or None


def _plain_float(value) -> Optional[float]:
    return float(value) if type(value) in (int, float) else None


def _plain_str(value) -> Optional[str]:
    """A str from the library's packet dict, or None - used for the enum-NAME
    fields (User.hwModel/role) MessageToDict already gives us as strings, and
    which it OMITS entirely at their proto3 default (confirmed directly
    against a bare User in verify_receive_topics.py) - so a missing key here
    is "not reported", not a false default value."""
    return value if type(value) is str else None


def _plain_bool(value) -> Optional[bool]:
    return value if type(value) is bool else None


def _node_id_from_num(num: int) -> str:
    return "^all" if num == _BROADCAST_NUM else f"!{num:08x}"


# Telemetry is a protobuf oneof: exactly one of these dict keys is present per
# packet (confirmed in verify_receive_topics.py). Maps the library's own key to
# ReceivedTelemetryEvent.kind.
_TELEMETRY_VARIANT_KEYS = (("deviceMetrics", "device"), ("environmentMetrics", "environment"), ("powerMetrics", "power"))


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

        # Serializes whole connect() calls (TCP lifecycle P0): unlike
        # self._lock (short state reads/writes only, must never be held
        # across a network call), this one IS held for the entire
        # connect() - so two callers reaching connect() directly can't
        # each open their own interface and leak the loser. In
        # production the adapter's single-threaded request loop already
        # serializes requests, so this is defense in depth for direct/
        # in-process users, not the primary guarantee.
        self._connect_lock = threading.Lock()

        self._interface = None
        self._state = _TcpState.DISCONNECTED
        self._connected_since: Optional[float] = None
        self._last_error: Optional[TransportError] = None
        self._node_id: Optional[str] = None

        # ---- inbound ----------------------------------------------------
        # Its own lock, never self._lock: the pubsub callback runs on the
        # library's publishing thread and must not wait behind a connect()/
        # send that holds the state lock.
        self._receive_lock = threading.Lock()
        self._receive_queue: collections.deque = collections.deque()
        self._receive_capacity = _RECEIVE_QUEUE_CAPACITY
        self._dropped_since_drain = 0
        self._malformed_since_drain = 0
        self._receive_stats = {
            "received_text": 0,
            "received_waypoint": 0,
            "received_nodeinfo": 0,
            "received_position": 0,
            "received_telemetry": 0,
            "queue_overflow_dropped": 0,
            "drained_events": 0,
            "malformed_events": 0,
        }
        self._last_overflow_log = float("-inf")
        # Bumps on every new physical connection. Observability only: the
        # safety check against writing radio A's events into radio B's profile
        # is the node id every event carries, not this.
        self._connection_generation = 0
        # The interface currently being CONSTRUCTED (its handshake is still in
        # flight and it is not self._interface yet) - a packet delivered in that
        # window is still ours.
        self._pending_interface = None
        # Subscribed exactly once per TCPTransport lifetime - NOT per connect()
        # or reconnect(), which would stack duplicate registrations. The
        # callbacks decide per event whether it came from the current interface.
        self._receive_subscribed = False
        self._receive_listeners: list = []
        self._receive_unavailable_reason: Optional[str] = None

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

    @staticmethod
    def _interface_is_healthy(interface) -> bool:
        """Whether an already-attached interface still looks alive - used
        by connect()'s idempotent shortcut so a stale READY (the radio
        rebooted, the reader thread died on a reset) is never mistaken for
        a live session. Reaches into the library's own _rxThread/
        isConnected the same way _detach_and_close_async() already does
        (this module already treats them as structured internals).
        Missing attributes are treated as "no evidence of a problem", so
        a library version that renames them degrades to "trust READY"
        rather than to "always reconnect"."""
        reader = getattr(interface, "_rxThread", None)
        if reader is not None and not reader.is_alive():
            return False
        connected = getattr(interface, "isConnected", None)
        if connected is not None and hasattr(connected, "is_set") and not connected.is_set():
            return False
        return True

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
            if interface_to_close is None:
                return
            try:
                interface_to_close.close()
            except Exception:
                pass
            # Adapter-watchdog follow-up (live-caught on pixel-111, T-Beam
            # firmware 2.7.15.567b8ea): the underlying meshtastic library's
            # own TCPInterface.close() only waits a short, hardcoded
            # interval (its own GRACEFUL_CLOSE_TIMEOUT, 0.25s at last
            # check) for its background reader thread to exit, and that
            # wait depends on the remote radio's own TCP stack reacting to
            # a half-close (shutdown(SHUT_WR)) promptly. A radio that
            # doesn't leaves the reader thread blocked in a blocking
            # recv() indefinitely - close() itself still returns (it
            # doesn't propagate that as a failure), but the raw socket
            # (and the radio's single-TCP-client slot) is silently still
            # held from our side. Live-reproduced as a clean alternating
            # pattern: every successful connect-then-disconnect cycle
            # left the NEXT connect attempt failing with TCP_CONNECTED,
            # every other one succeeding - exactly what a leaked reader
            # thread from every other disconnect() would produce.
            #
            # If the thread is confirmed still alive after the library's
            # own attempt, force a hard close of the raw socket ourselves
            # - reaching into the library's own _rxThread/socket
            # attributes is a deliberate, narrow reach past its public
            # API for exactly this gap (this module already treats the
            # library as structured internals elsewhere - see
            # _local_node_id()/get_local_node() reading myInfo/nodes
            # directly - not a novel pattern introduced here), not a
            # workaround of convenience. An abrupt socket.close() raises
            # inside a blocked recv() as a plain OSError regardless of
            # what the remote side ever does, unblocking the thread
            # immediately instead of waiting on it.
            reader_thread = getattr(interface_to_close, "_rxThread", None)
            if reader_thread is not None and reader_thread.is_alive():
                raw_socket = getattr(interface_to_close, "socket", None)
                if raw_socket is not None:
                    try:
                        raw_socket.close()
                    except Exception:
                        pass

        try:
            self._call_with_timeout(_do_close, timeout=timeout, what="close interface")
        except TransportError as error:
            # Own state is already correct (detached above) regardless -
            # this is purely "how long did the background close() take",
            # not a correctness signal, so it's logged, not re-raised.
            self._on_log(f"TCP interface close() did not finish within {timeout}s: {error}", "WARNING")

    def _open_socket(self, timeout: float) -> socket.socket:
        """Open THE TCP connection to (self._host, self._port) and hand it
        to the Meshtastic library (see _open_interface's `preopened_socket`).

        This used to be a throwaway probe: open a socket, close it, then let
        TCPInterface open a second one. A radio serves one client, and a
        connect right behind a connect-and-close makes it reset the real
        handshake - measured on the T-Beam (pixel-111, 80 cycles): probe +
        handshake in one process succeeded 1 of 32 times, the handshake
        alone 26 of 32, a fresh process per connect 14 of 16, at every pause
        from 0 to 10s before the probe. It also explains why boot connects
        and every force-reconnect kept failing "immediately" with
        tcp_connected: they all went through the probe.

        Doing the connect ourselves keeps what the probe was for - distinct
        dns_error / connect_refused / connect_timeout codes within a bounded
        10s, entirely independent of the Meshtastic protocol layer (stdlib
        `socket` only, no GPLv3 import, testable with a monkeypatched
        socket.create_connection) - with exactly ONE TCP connection per
        connect().

        The caller owns the returned socket until the interface adopts it
        (or must close it on failure)."""
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
        # Connected with a bounded timeout; the library's reader thread does
        # blocking recv() on it, so it must be back to blocking mode.
        probe_socket.settimeout(None)
        return probe_socket

    @staticmethod
    def _close_quietly(sock) -> None:
        try:
            sock.close()
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
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            # H1-C4(b): checked BEFORE the generic OSError branch below (a
            # BrokenPipeError/ConnectionResetError IS an OSError, so it
            # would otherwise be swallowed into the generic CONNECT_FAILED
            # path with a raw, unexplained "Broken pipe" string). This
            # early in the handshake - right after _open_socket() already
            # proved the raw TCP connect itself succeeded - it is the
            # characteristic signature of a radio that serves exactly one
            # TCP client (see TransportErrorCode.TCP_RADIO_BUSY's own
            # docstring) accepting our socket and then immediately killing
            # it because another client already holds its one slot.
            return TransportError(
                TransportErrorCode.TCP_RADIO_BUSY,
                f"{self._host}:{self._port} closed the connection immediately after accepting it "
                f"(radio busy: another client is connected): {exc}",
            )
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

    def _open_interface(self, preopened_socket=None):
        """The one lazy `meshtastic` import in this module - see the
        module docstring's lazy-import-discipline note. `connectNow=True`
        (TCPInterface's own default) is what makes this call block for
        the FULL raw-connect + protocol-handshake sequence, the same way
        SerialInterface's constructor already does for SerialTransport -
        see that module's connect() docstring for the equivalent proof-
        of-connectivity reasoning this mirrors for TCP.

        Returns a _FailFastTCPInterface (defined locally, not at module
        scope - the lazy-import discipline above means TCPInterface
        can't be named until this function actually runs) instead of
        the plain TCPInterface class - see that subclass's own
        docstring for the root cause this fixes and why the fix is
        safe. Raises plain RuntimeError, not meshtastic.mesh_interface.
        MeshInterface.MeshInterfaceError (what the real library itself
        raises on this same timeout) - deliberately avoids a second
        `meshtastic` import: _classify_sync_failure() (this module)
        only distinguishes "was it a socket-layer OSError" from
        "anything else", so any plain Exception subtype classifies
        identically to TCP_CONNECTED. This also keeps
        tests/test_tcp_transport.py's fully-mocked suite working - it
        stubs sys.modules["meshtastic.tcp_interface"] only (fakes
        TCPInterface itself), not "meshtastic.mesh_interface", and
        Core's own venv/CI has no real `meshtastic` package installed
        at all (see CLAUDE.md's GPLv3 process isolation section) - a
        second top-level `from meshtastic.mesh_interface import ...`
        here would ModuleNotFoundError in exactly that environment."""
        from meshtastic.tcp_interface import TCPInterface

        owner = self

        class _FailFastTCPInterface(TCPInterface):
            """Overrides MeshInterface._waitConnected()'s single 30s
            Event.wait() call with a short poll loop that also watches
            the reader thread's own aliveness.

            ROOT CAUSE (confirmed by reading the pinned meshtastic==2.7.11
            source directly, live-caught on pixel-111, T-Beam firmware
            2.7.15.567b8ea): _waitConnected() blocks on
            self.isConnected.wait(timeout=30.0) - a threading.Event only
            ever .set() from _connected() (mesh_interface.py), which only
            runs once the config download has genuinely, fully succeeded.
            On a reader-thread death (e.g. the real "Connection reset by
            peer" observed in production), stream_interface.py's own
            __reader() loop DOES call self._disconnected() from its
            `finally:` block - but _disconnected() only ever calls
            self.isConnected.clear(), never .set(). Event.clear() never
            wakes a thread already blocked in .wait() - it is a pure
            no-op from the waiter's perspective when the Event was never
            set in the first place, which is exactly the case for a
            connect attempt that's failing (it never reached
            _connected()). Net effect: a dead reader thread leaves
            _waitConnected() blocked for the FULL hardcoded 30s
            regardless, observed live as a consistent ~35s hang (30s
            wait + a few seconds of TCP-probe/raw-connect overhead)
            ending in AdapterSupervisor's own outer SIGKILL of the whole
            adapter subprocess, not a clean TransportError.

            FIX: poll self.isConnected (success path, functionally
            identical to the original - a slow-but-healthy handshake
            waits out the exact same total budget, just observed in
            0.25s steps instead of one blocking call) and
            self._rxThread.is_alive() (failure path - only fires once
            the reader thread has UNAMBIGUOUSLY exited, never merely
            "hasn't produced data yet", so this can never rush a
            legitimate slow connect - see
            tests/test_tcp_transport_integration.py's stalled-handshake
            test, which stays green unchanged under this override since
            that scenario's reader thread stays alive, blocked in
            recv(), the entire time). `reader_thread.ident is not None`
            additionally guards against a thread object that exists but
            hasn't actually been started yet (never observed in practice
            here - connect() always calls self._rxThread.start() before
            _waitConnected() - kept as an explicit, cheap safety check
            rather than an assumption). `not self._wantExit` excludes an
            intentional concurrent close() (e.g. a racing force=True
            reconnect) from being misclassified as a fatal failure - the
            same guard stream_interface.py's own code uses everywhere
            else for this exact reason.

            FRAGILITY, ACKNOWLEDGED: this overrides a private
            (underscore-prefixed) method of a pinned third-party library
            (meshtastic>=2.7.9,<2.8.0 - see adapters/meshtastic/
            requirements.txt's own pin rationale). A future version bump
            within that range could change _waitConnected()'s shape and
            silently turn this override into a no-op (falling back to
            TCPInterface's own default method, i.e. today's 30s-hang
            behavior returns, quietly). Guarded by
            tests/test_tcp_transport_integration.py's dedicated
            regression test asserting this override actually fires
            within ~1s against a real reader-thread death (not just that
            the code imports/runs) - a version bump that broke this
            would fail that test in CI, not surface live again."""

            _WAIT_POLL_INTERVAL_S = 0.25

            def __init__(self, *args, **kwargs):
                # Registered BEFORE the constructor runs its handshake: the
                # object is not self._interface until the handshake finishes,
                # and a receive callback in that window must still recognise it.
                owner._pending_interface = self
                self._preopened_socket_consumed = False
                super().__init__(*args, **kwargs)

            def myConnect(self):
                """Adopt the already-open socket instead of letting the
                library dial a second connection (see _open_socket()) - but
                ONLY on THIS construction's own first connect.

                LIVE REGRESSION (pixel-111, 2026-09-27): myConnect() used to
                reuse `preopened_socket` unconditionally, on every call - but
                the library calls it a second time on this SAME instance
                whenever ITS OWN internal self-healing fires:
                _writeBytes()/_readBytes() catch a raw OSError (a dead/closed
                fd - the exact "Bad file descriptor" observed live) and call
                self._reconnect(), which closes self.socket and calls
                self.myConnect() again to get a fresh one. Reusing the
                already-dead preopened_socket there instead of dialling a real
                new connection turned that self-healing path into an infinite
                once-a-second reconnect-fail loop (time.sleep(1) inside the
                library's own _reconnect()) that no traffic could ever break
                out of - only a full service restart did. The one-connection
                guarantee this override exists for only concerns OUR OWN
                initial connect() (_open_interface()'s single call site always
                supplies a freshly probed socket for that); the library's own
                later reconnect attempts must keep using its real, unmodified
                myConnect() so they can actually succeed."""
                if preopened_socket is not None and not self._preopened_socket_consumed:
                    self.socket = preopened_socket
                    self._preopened_socket_consumed = True
                else:
                    super().myConnect()

            def _waitConnected(self, timeout=30.0):
                if self.noProto:
                    return
                deadline = time.monotonic() + timeout
                while True:
                    if self.isConnected.wait(self._WAIT_POLL_INTERVAL_S):
                        break
                    reader_thread = self._rxThread
                    if (
                        reader_thread is not None
                        and reader_thread.ident is not None
                        and not reader_thread.is_alive()
                        and not self._wantExit
                    ):
                        raise RuntimeError(
                            "TCP reader thread exited before the connection "
                            "completed (fail-fast override - see "
                            "_FailFastTCPInterface's docstring in "
                            "adapters/meshtastic/tcp_transport.py)"
                        )
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Timed out waiting for connection completion"
                        )

                if self.failure:
                    raise self.failure

        return _FailFastTCPInterface(hostname=self._host, portNumber=self._port, connectNow=True)

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
        acceptance tests against the T-Beam have actually run.

        IDEMPOTENT (TCP lifecycle P0): `force=False` used to flip state to
        CONNECTING (so every send failed not_connected meanwhile), open a
        raw probe socket AND a second full TCPInterface - two TCP clients
        from us to a radio that effectively serves one - and then
        overwrite self._interface without closing the old one, leaking its
        socket and reader thread. Now:
          - already READY to the same host:port with a healthy interface
            -> return the current ConnectionInfo, touch nothing;
          - anything else with an interface attached (a different
            endpoint, DEGRADED, or a READY whose reader thread has died)
            -> that interface is closed first, via the same
            _detach_and_close_async() path force=True already used;
          - force=True always reconnects (reconnect() depends on it).
        Whole calls are serialized by self._connect_lock."""
        if descriptor.type != ConnectionType.TCP:
            raise TransportError(
                TransportErrorCode.UNSUPPORTED, f"TCPTransport cannot connect to {descriptor.type}"
            )

        if not self._connect_lock.acquire(timeout=max(1.0, timeout)):
            raise TransportError(
                TransportErrorCode.BUSY, "another connect() is already in progress on this TCPTransport"
            )
        try:
            target_host, target_port = _parse_host_port(
                descriptor.address, default_port=self._port or DEFAULT_TCP_PORT
            )
            if not force:
                with self._lock:
                    same_endpoint = (
                        (target_host or self._host) == self._host
                        and (target_port or self._port) == self._port
                    )
                    reusable = (
                        same_endpoint
                        and self._state == _TcpState.READY
                        and self._interface is not None
                        and self._interface_is_healthy(self._interface)
                    )
                    if reusable:
                        self._label = descriptor.label or self._label
                if reusable:
                    return self.get_connection_info()
            return self._connect_locked(descriptor, force=force, timeout=timeout)
        finally:
            self._connect_lock.release()

    def _connect_locked(
        self, descriptor: ConnectionDescriptor, *, force: bool, timeout: float
    ) -> ConnectionInfo:
        """The original connect() body - caller holds self._connect_lock."""

        # Snapshot before any mutation, restored on every failure path
        # below - same rollback discipline as BLETransport.connect() (see
        # that module's own docstring for the live TAP2 finding this
        # guards against): a failed connect() must never leave
        # self._host/_port pointing at the address that just failed, or a
        # later bare reconnect() (which rebuilds its descriptor from
        # these fields, not a fresh caller-supplied one) would retry the
        # bad address forever instead of the last-known-good one.
        previous_host, previous_port, previous_label = self._host, self._port, self._label

        self._ensure_receive_subscription()

        host, port = _parse_host_port(descriptor.address, default_port=self._port or DEFAULT_TCP_PORT)
        self._host = host or self._host
        self._port = port or self._port
        self._label = descriptor.label or self._label

        with self._lock:
            self._state = _TcpState.CONNECTING
            has_interface = self._interface is not None

        if force or has_interface:
            self._detach_and_close_async(timeout=timeout)

        probe_timeout = min(timeout, _TCP_PROBE_TIMEOUT_S)
        try:
            connected_socket = self._open_socket(probe_timeout)
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
                interface = self._open_interface(connected_socket)
            except Exception as exc:
                self._close_quietly(connected_socket)
                raise self._classify_sync_failure(exc) from exc
            if getattr(interface, "socket", None) is not connected_socket:
                # The library did not adopt our socket (a library version that
                # no longer calls myConnect(), or a fake): it dialled its own.
                # Don't leave ours dangling as a second client.
                self._close_quietly(connected_socket)
            return interface

        try:
            interface = self._call_with_timeout(_do_sync, timeout=remaining, what="connect() protocol sync")
        except TransportError as error:
            self._pending_interface = None
            self._close_quietly(connected_socket)
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
            displaced = self._interface
            self._interface = interface
            self._pending_interface = None
            self._node_id = node_id
            self._state = _TcpState.READY
            self._connected_since = time.time()
            self._last_error = None
            self._connection_generation += 1
        if displaced is not None and displaced is not interface:
            # Defensive only - _connect_lock plus the detach above mean
            # nothing should still be attached here, but silently
            # overwriting (the pre-fix behavior) would leak its socket and
            # reader thread if that invariant ever breaks.
            try:
                displaced.close()
            except Exception:
                pass
        return self.get_connection_info()

    def disconnect(self, *, timeout: float = 15.0) -> None:
        self._detach_and_close_async(timeout=timeout)

    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        """Bounded backoff per this feature's own spec (see
        _RECONNECT_DELAYS_S) - full handshake + identity re-validation on
        every attempt, via connect(force=True), never a lighter-weight
        "just reopen the socket" path: a reconnect that skips identity
        re-validation could silently start talking to a different radio
        than the one this transport was originally bound to.

        Needs a known endpoint: a freshly (re)spawned adapter process
        builds this transport with host="" (ipc_server.py) and only learns
        the address from connect() or adopt_endpoint(). Without one, every
        one of the 6 attempts used to fail with `dns_error: could not
        resolve ''` and the backoff sleeps (~48s) ran while Core's router
        lock was held (live-caught on pixel-111: the futile auto-reconnect
        even made a manual switch() BUSY). Fail immediately instead."""
        if not self._host:
            raise TransportError(
                TransportErrorCode.CONNECT_FAILED,
                "no remembered TCP endpoint (this adapter process was restarted) - "
                "reconnect needs an explicit address; use connect() or pass one from Core",
            )
        # ONE shared, shrinking budget. Every attempt used to be handed the
        # caller's whole `timeout` again, so six slow attempts plus ~108s of
        # backoff could run for many multiples of it - past the outer
        # AdapterSupervisor deadline (which then SIGKILLs the adapter mid-
        # attempt and loses this transport's state) and for the whole time
        # under Core's router lock.
        deadline = time.monotonic() + timeout

        with self._lock:
            self._state = _TcpState.RECONNECTING
        self.disconnect(timeout=min(timeout, 15.0))

        last_error: Optional[TransportError] = None
        attempts = len(_RECONNECT_DELAYS_S)
        made = 0
        for attempt, base_delay in enumerate(_RECONNECT_DELAYS_S, start=1):
            remaining = deadline - time.monotonic()
            if made and remaining < _RECONNECT_MIN_ATTEMPT_S:
                break
            made += 1
            try:
                return self.connect(
                    ConnectionDescriptor(
                        type=ConnectionType.TCP, address=f"{self._host}:{self._port}", label=self._label
                    ),
                    force=True,
                    timeout=max(1.0, remaining),
                )
            except TransportError as error:
                last_error = error
                self._on_log(f"TCP reconnect attempt {attempt}/{attempts} failed: {error}", "WARNING")
                if attempt < attempts:
                    # Sleep only if enough budget is left afterwards for
                    # another real attempt; never sleep into the deadline.
                    room = deadline - time.monotonic() - _RECONNECT_MIN_ATTEMPT_S
                    delay = min(self._jittered_delay(base_delay), room)
                    if delay <= 0:
                        self._on_log(
                            f"TCP reconnect budget ({timeout}s) exhausted after {made} attempt(s)", "WARNING"
                        )
                        break
                    time.sleep(delay)

        with self._lock:
            self._state = _TcpState.ERROR
            self._last_error = last_error
        raise last_error or TransportError(TransportErrorCode.CONNECT_FAILED, "reconnect() exhausted all attempts")

    @staticmethod
    def _jittered_delay(base_delay: float) -> float:
        jitter = base_delay * random.uniform(-_RECONNECT_JITTER_RATIO, _RECONNECT_JITTER_RATIO)
        return max(_RECONNECT_MIN_DELAY_S, base_delay + jitter)

    def adopt_endpoint(self, descriptor: ConnectionDescriptor) -> None:
        """Core re-supplies the endpoint on reconnect (ipc_server.py's
        `reconnect` operation): an adapter process restarted since the last
        connect() has no memory of it. Only ever fills/updates the address;
        never touches a live session's state. A non-TCP or empty descriptor
        is ignored."""
        if descriptor is None or descriptor.type != ConnectionType.TCP:
            return
        host, port = _parse_host_port(descriptor.address, default_port=self._port or DEFAULT_TCP_PORT)
        if not host:
            return
        with self._lock:
            self._host = host
            self._port = port or self._port
            self._label = descriptor.label or self._label

    def _demote_if_dead_locked(self) -> None:
        """Caller holds self._lock. A READY session whose reader thread has
        died (a `Connection reset by peer` ends the library's reader, and the
        library's heartbeat then just hits BrokenPipe every few minutes) used
        to stay READY - reported CONNECTED forever - until something happened
        to send; `_state` only flipped on a failed send. Live evidence
        (pixel-111, 2026-09-25): reset at 21:54, then heartbeat BrokenPipe
        every 10 minutes until a manual restart. Flipped to DEGRADED here so
        the health worker sees ERROR and auto-reconnect can act. Only looks
        at in-memory thread/event state - no I/O."""
        if self._state != _TcpState.READY or self._interface is None:
            return
        if self._interface_is_healthy(self._interface):
            return
        self._state = _TcpState.DEGRADED
        self._connected_since = None
        self._last_error = TransportError(
            TransportErrorCode.REMOTE_DISCONNECT,
            f"TCP connection to {self._host}:{self._port} was lost (reader thread exited)",
        )

    def is_connected(self) -> bool:
        with self._lock:
            self._demote_if_dead_locked()
            return self._state == _TcpState.READY and self._interface is not None

    def get_connection_info(self) -> ConnectionInfo:
        with self._lock:
            self._demote_if_dead_locked()
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
        self._unsubscribe_receive()
        self._shutdown_executor()

    # ------------------------------------------------------------------
    # Inbound: pubsub capture -> normalize -> bounded queue -> drain_received()
    # ------------------------------------------------------------------
    def _ensure_receive_subscription(self) -> None:
        """Subscribe to the two receive topics, once for this instance's
        lifetime. `pubsub` is the library's own dependency, imported lazily
        like `meshtastic` itself (this module is imported in environments that
        have neither); if it is missing, receiving is reported UNSUPPORTED
        rather than breaking connect()."""
        with self._receive_lock:
            if self._receive_subscribed or self._receive_unavailable_reason:
                return
            try:
                from pubsub import pub
            except ImportError as error:
                self._receive_unavailable_reason = f"pubsub is not importable: {error}"
                self._on_log(f"TCP receive disabled: {self._receive_unavailable_reason}", "WARNING")
                return
            listeners = [
                (_RECEIVE_TOPIC_TEXT, self._on_receive_text),
                (_RECEIVE_TOPIC_WAYPOINT, self._on_receive_waypoint),
                (_RECEIVE_TOPIC_NODEINFO, self._on_receive_nodeinfo),
                (_RECEIVE_TOPIC_POSITION, self._on_receive_position),
                (_RECEIVE_TOPIC_TELEMETRY, self._on_receive_telemetry),
            ]
            for topic, listener in listeners:
                pub.subscribe(listener, topic)
            # pypubsub keeps only weak references to listeners: hold them.
            self._receive_listeners = listeners
            self._receive_subscribed = True

    def _unsubscribe_receive(self) -> None:
        with self._receive_lock:
            listeners, self._receive_listeners = self._receive_listeners, []
            was_subscribed, self._receive_subscribed = self._receive_subscribed, False
        if not was_subscribed:
            return
        try:
            from pubsub import pub
        except ImportError:
            return
        for topic, listener in listeners:
            try:
                pub.unsubscribe(listener, topic)
            except Exception:
                pass

    def _is_current_interface(self, interface) -> bool:
        """`pub` is process-global and this adapter process also hosts the
        Serial and BLE transports: an event is ours only if it came from OUR
        current (or currently-being-built) interface - never from another
        transport's, or from one this transport has already let go of."""
        return interface is not None and (
            interface is self._interface or interface is self._pending_interface
        )

    def _on_receive_text(self, packet, interface) -> None:
        self._on_receive(packet, interface, "text", self._normalize_text)

    def _on_receive_waypoint(self, packet, interface) -> None:
        self._on_receive(packet, interface, "waypoint", self._normalize_waypoint)

    def _on_receive_nodeinfo(self, packet, interface) -> None:
        self._on_receive(packet, interface, "nodeinfo", self._normalize_nodeinfo)

    def _on_receive_position(self, packet, interface) -> None:
        self._on_receive(packet, interface, "position", self._normalize_position)

    def _on_receive_telemetry(self, packet, interface) -> None:
        self._on_receive(packet, interface, "telemetry", self._normalize_telemetry)

    def _on_receive(self, packet, interface, kind, normalize) -> None:
        """Runs on the library's publishing thread: must never raise into it."""
        try:
            if not self._is_current_interface(interface):
                return
            event = normalize(packet, interface)
            if event is not None:
                self._enqueue_received(event, kind)
        except Exception:
            # One undecodable packet must not cost anything else - count it.
            # (No packet content is logged: message text is private.)
            with self._receive_lock:
                self._malformed_since_drain += 1
                self._receive_stats["malformed_events"] += 1

    def _enqueue_received(self, event, kind: str) -> None:
        log_overflow = False
        with self._receive_lock:
            if len(self._receive_queue) >= self._receive_capacity:
                self._receive_queue.popleft()  # drop the OLDEST, keep the freshest
                self._dropped_since_drain += 1
                self._receive_stats["queue_overflow_dropped"] += 1
                now = time.monotonic()
                if now - self._last_overflow_log >= _RECEIVE_OVERFLOW_LOG_INTERVAL_S:
                    self._last_overflow_log = now
                    log_overflow = True
            self._receive_queue.append(event)
            self._receive_stats[f"received_{kind}"] += 1
            dropped_total = self._receive_stats["queue_overflow_dropped"]
        if log_overflow:
            self._on_log(
                f"TCP receive queue full ({self._receive_capacity}): dropping oldest events "
                f"({dropped_total} dropped so far) - Core is not draining fast enough",
                "WARNING",
            )

    def _normalize_text(self, packet, interface) -> Optional[ReceivedTextEvent]:
        """Explicit field-by-field construction. `raw` (a protobuf MeshPacket),
        `decoded.payload` (bytes) and everything else in the library's dict are
        never read, let alone carried. Returns None for an empty text (parity
        with the serial parser, which skips it); raises for a malformed packet."""
        decoded = packet["decoded"]
        text = decoded["text"]
        if not isinstance(text, str):
            raise TypeError("decoded.text is not a str")
        if not text.strip():
            return None
        from_num = packet["from"]
        to_num = packet["to"]
        if type(from_num) is not int or type(to_num) is not int:
            raise TypeError("from/to are not integers")
        local_node_id = self._local_node_id(interface)
        if not local_node_id:
            raise ValueError("local radio node id is not known yet")
        channel = _plain_int(packet.get("channel"))
        return ReceivedTextEvent(
            from_node_id=f"!{from_num:08x}",
            to_node_id=_node_id_from_num(to_num),
            text=text,
            received_at=time.time(),
            local_radio_node_id=local_node_id,
            packet_id=_plain_int_or_none_if_zero(packet.get("id")),
            from_num=from_num,
            to_num=to_num,
            channel_index=0 if channel is None else channel,  # protobuf default 0 is omitted
            reply_id=_plain_int_or_none_if_zero(decoded.get("replyId")),
            rx_time=_plain_int_or_none_if_zero(packet.get("rxTime")),
            rx_rssi=_plain_int_or_none_if_zero(packet.get("rxRssi")),
            rx_snr=_plain_float(packet.get("rxSnr")),
            hop_limit=_plain_int(packet.get("hopLimit")),
            hop_start=_plain_int(packet.get("hopStart")),
            relay_node=_plain_int_or_none_if_zero(packet.get("relayNode")),
        )

    def _normalize_waypoint(self, packet, interface) -> Optional[ReceivedWaypointEvent]:
        """Same rules as _normalize_text; coordinates arrive as latitudeI /
        longitudeI (1e-7 degrees) and become plain floats. The waypoint dict's
        own `raw` (a protobuf text string) is never read."""
        waypoint = packet["decoded"]["waypoint"]
        waypoint_id = _plain_int(waypoint.get("id"))
        if waypoint_id is None:
            raise ValueError("waypoint has no integer id")
        from_num = packet["from"]
        if type(from_num) is not int:
            raise TypeError("from is not an integer")
        name = waypoint.get("name", "")
        description = waypoint.get("description", "")
        if not isinstance(name, str) or not isinstance(description, str):
            raise TypeError("waypoint name/description are not str")
        local_node_id = self._local_node_id(interface)
        if not local_node_id:
            raise ValueError("local radio node id is not known yet")
        latitude_i = _plain_int(waypoint.get("latitudeI"))
        longitude_i = _plain_int(waypoint.get("longitudeI"))
        channel = _plain_int(packet.get("channel"))
        return ReceivedWaypointEvent(
            waypoint_id=waypoint_id,
            sender_id=f"!{from_num:08x}",
            name=name,
            description=description,
            received_at=time.time(),
            local_radio_node_id=local_node_id,
            packet_id=_plain_int_or_none_if_zero(packet.get("id")),
            latitude=None if latitude_i is None else latitude_i / 1e7,
            longitude=None if longitude_i is None else longitude_i / 1e7,
            icon=_plain_int(waypoint.get("icon")),
            expire_at=_plain_int(waypoint.get("expire")),
            channel_index=0 if channel is None else channel,
        )

    def _normalize_nodeinfo(self, packet, interface) -> Optional[ReceivedNodeInfoEvent]:
        """Same rules as _normalize_text. `hwModel`/`role` are already enum
        NAME strings from the library (never the raw int); a proto3 default
        (role CLIENT, isLicensed False, ...) is OMITTED from the dict entirely
        (verify_receive_topics.py confirmed this against a bare User), so a
        missing key here means "not reported", not a default value - read with
        `_plain_str`/`_plain_bool`, never a falsy-default `.get(..., "")`.
        `macaddr`/`publicKey` are read by nothing here - see
        ReceivedNodeInfoEvent's own docstring for why they are not carried."""
        user = packet["decoded"]["user"]
        node_id = user.get("id")
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("user has no id")
        from_num = packet["from"]
        if type(from_num) is not int:
            raise TypeError("from is not an integer")
        local_node_id = self._local_node_id(interface)
        if not local_node_id:
            raise ValueError("local radio node id is not known yet")
        channel = _plain_int(packet.get("channel"))
        return ReceivedNodeInfoEvent(
            node_id=node_id,
            sender_id=f"!{from_num:08x}",
            received_at=time.time(),
            local_radio_node_id=local_node_id,
            packet_id=_plain_int_or_none_if_zero(packet.get("id")),
            long_name=_plain_str(user.get("longName")),
            short_name=_plain_str(user.get("shortName")),
            hw_model=_plain_str(user.get("hwModel")),
            role=_plain_str(user.get("role")),
            is_licensed=_plain_bool(user.get("isLicensed")),
            channel_index=0 if channel is None else channel,
            rx_time=_plain_int_or_none_if_zero(packet.get("rxTime")),
            rx_rssi=_plain_int_or_none_if_zero(packet.get("rxRssi")),
            rx_snr=_plain_float(packet.get("rxSnr")),
            hop_limit=_plain_int(packet.get("hopLimit")),
            hop_start=_plain_int(packet.get("hopStart")),
            relay_node=_plain_int_or_none_if_zero(packet.get("relayNode")),
        )

    def _normalize_position(self, packet, interface) -> Optional[ReceivedPositionEvent]:
        """Same rules as _normalize_text. Unlike Waypoint, `latitude`/
        `longitude` are already plain floats here - the library's own
        _fixupPosition converts latitudeI/longitudeI (1e-7 deg) before
        publishing, confirmed in verify_receive_topics.py - so no manual /1e7
        division happens on this side."""
        position = packet["decoded"]["position"]
        from_num = packet["from"]
        if type(from_num) is not int:
            raise TypeError("from is not an integer")
        local_node_id = self._local_node_id(interface)
        if not local_node_id:
            raise ValueError("local radio node id is not known yet")
        channel = _plain_int(packet.get("channel"))
        return ReceivedPositionEvent(
            sender_id=f"!{from_num:08x}",
            received_at=time.time(),
            local_radio_node_id=local_node_id,
            packet_id=_plain_int_or_none_if_zero(packet.get("id")),
            latitude=_plain_float(position.get("latitude")),
            longitude=_plain_float(position.get("longitude")),
            altitude=_plain_int(position.get("altitude")),
            ground_speed=_plain_int(position.get("groundSpeed")),
            sats_in_view=_plain_int(position.get("satsInView")),
            position_time=_plain_int(position.get("time")),
            channel_index=0 if channel is None else channel,
            rx_time=_plain_int_or_none_if_zero(packet.get("rxTime")),
            rx_rssi=_plain_int_or_none_if_zero(packet.get("rxRssi")),
            rx_snr=_plain_float(packet.get("rxSnr")),
            hop_limit=_plain_int(packet.get("hopLimit")),
            hop_start=_plain_int(packet.get("hopStart")),
            relay_node=_plain_int_or_none_if_zero(packet.get("relayNode")),
        )

    def _normalize_telemetry(self, packet, interface) -> Optional[ReceivedTelemetryEvent]:
        """Same rules as _normalize_text. Telemetry is a protobuf oneof -
        exactly one of deviceMetrics/environmentMetrics/powerMetrics is
        present per packet (verify_receive_topics.py); a packet naming none of
        our three known variants (e.g. a newer airQualityMetrics/localStats/
        healthMetrics/hostMetrics field this stage does not support) is
        treated as malformed rather than guessed at - out of scope, not
        silently coerced. `metrics` is handed to ReceivedTelemetryEvent as-is;
        its own constructor validates every value is a plain int/float."""
        telemetry = packet["decoded"]["telemetry"]
        metrics = None
        kind = None
        for variant_key, variant_kind in _TELEMETRY_VARIANT_KEYS:
            if variant_key in telemetry:
                metrics = telemetry[variant_key]
                kind = variant_kind
                break
        if kind is None:
            raise ValueError(f"telemetry packet has no known variant: {sorted(telemetry)}")
        from_num = packet["from"]
        if type(from_num) is not int:
            raise TypeError("from is not an integer")
        local_node_id = self._local_node_id(interface)
        if not local_node_id:
            raise ValueError("local radio node id is not known yet")
        channel = _plain_int(packet.get("channel"))
        return ReceivedTelemetryEvent(
            sender_id=f"!{from_num:08x}",
            kind=kind,
            metrics=metrics,
            received_at=time.time(),
            local_radio_node_id=local_node_id,
            packet_id=_plain_int_or_none_if_zero(packet.get("id")),
            telemetry_time=_plain_int(telemetry.get("time")),
            channel_index=0 if channel is None else channel,
            rx_time=_plain_int_or_none_if_zero(packet.get("rxTime")),
            rx_rssi=_plain_int_or_none_if_zero(packet.get("rxRssi")),
            rx_snr=_plain_float(packet.get("rxSnr")),
            hop_limit=_plain_int(packet.get("hopLimit")),
            hop_start=_plain_int(packet.get("hopStart")),
            relay_node=_plain_int_or_none_if_zero(packet.get("relayNode")),
        )

    def drain_received(self, *, limit: int = 100, timeout: float = 5.0) -> ReceivedBatch:
        """Take up to `limit` buffered events, oldest first, plus how many were
        dropped (queue overflow) and how many could not be decoded since the
        previous drain. Reads memory only - no radio I/O, no TCP - so polling it
        about once a second costs the radio nothing. The queue is NOT cleared by
        disconnect()/reconnect(): events already captured survive a reconnect to
        the same radio (they die only with the adapter process)."""
        if self._receive_unavailable_reason:
            raise TransportError(TransportErrorCode.UNSUPPORTED, self._receive_unavailable_reason)
        if type(limit) is not int or limit < 1:
            raise TransportError(TransportErrorCode.UNKNOWN, f"drain_received limit must be a positive int, got {limit!r}")
        with self._receive_lock:
            count = min(limit, len(self._receive_queue))
            events = tuple(self._receive_queue.popleft() for _ in range(count))
            dropped, self._dropped_since_drain = self._dropped_since_drain, 0
            malformed, self._malformed_since_drain = self._malformed_since_drain, 0
            self._receive_stats["drained_events"] += count
            generation = self._connection_generation
        return ReceivedBatch(events=events, dropped=dropped, malformed=malformed, connection_generation=generation)

    def get_receive_stats(self) -> dict:
        """Counters for observability (no message content): received_text,
        received_waypoint, received_nodeinfo, received_position,
        received_telemetry, queue_depth, queue_overflow_dropped,
        drained_events, malformed_events."""
        with self._receive_lock:
            stats = dict(self._receive_stats)
            stats["queue_depth"] = len(self._receive_queue)
        return stats

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
            position=json_safe(data.get("position")),
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
