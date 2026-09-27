"""Backend Protocol v1 — the transport-neutral contract Core uses to talk to
a Meshtastic radio, regardless of whether the concrete implementation reaches
it over USB serial or BLE (and, later, TCP).

Interface only (Task 43.5) — no implementation here, and nothing in this
module imports `meshtastic` or references protobuf/`SerialInterface`/
`BLEInterface` types. See docs/BACKEND_API.md for the JSON wire shape this
maps onto once the adapter crosses a process boundary (Task 48), and for the
Task 43 findings the timeout/reconnect contracts below codify.
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence, Union

PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ConnectionType(str, Enum):
    SERIAL = "serial"
    BLUETOOTH = "bluetooth"
    TCP = "tcp"


class ConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class TransportErrorCode(str, Enum):
    NOT_CONNECTED = "not_connected"
    CONNECT_FAILED = "connect_failed"
    IDENTITY_MISMATCH = "identity_mismatch"
    TIMEOUT = "timeout"
    BUSY = "busy"
    DEVICE_NOT_FOUND = "device_not_found"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    # Task 48: distinct from NOT_CONNECTED/CONNECT_FAILED, which both mean
    # "we reached the adapter and it told us a radio-level thing failed."
    # This means the adapter subprocess ITSELF was never reached at all -
    # never started, failed to start, or crashed before ever completing a
    # round-trip. Matches the plan document's own acceptance-test wording
    # ("статус транспорта — 'adapter unavailable'", pip-uninstall-meshtastic
    # scenario) - deliberately not reusing UNKNOWN, so a caller/UI can tell
    # "radio not connected" apart from "the whole adapter is missing" at a
    # glance, not by string-matching last_error.message.
    ADAPTER_UNAVAILABLE = "adapter_unavailable"
    # P0 stabilization follow-up (Droidian-caught stdout-corruption
    # cascade): distinct from ADAPTER_UNAVAILABLE (adapter never reached/
    # crashed/closed its pipe) and UNKNOWN (a genuinely nondescript
    # failure) - this means the adapter WAS reached and responded, but
    # what came back on stdout wasn't valid JSON after tolerating a
    # bounded number of stray lines (AdapterSupervisor.call()'s reader -
    # see MAX_NON_JSON_LINES/MAX_NON_JSON_BYTES there). A caller/UI can
    # tell "the wire protocol itself broke" apart from "a radio-level
    # thing failed" or "the adapter is missing entirely" at a glance, not
    # by string-matching last_error.message.
    ADAPTER_PROTOCOL_ERROR = "adapter_protocol_error"
    # Droidian-caught follow-up to the P0.3 asymmetric-trust work: a
    # port-release check that couldn't reach a definitive answer (an
    # external tool like `lsof` timed out/errored/is missing) is NOT the
    # same thing as a confirmed busy port - claim_exclusive_access() used
    # to raise BUSY for both, a false "Serial port busy" the user could
    # not distinguish from a genuinely occupied port. Distinct from BUSY
    # (a real owner was found) so a caller/UI can tell "we don't know"
    # apart from "something is actually holding it".
    PORT_CHECK_INCONCLUSIVE = "port_check_inconclusive"
    # Radio TCP Transport (part 1): five new codes, all specific to
    # TCPTransport's explicit two-stage connect (raw TCP socket open,
    # THEN the Meshtastic protocol handshake/config sync) - see
    # adapters/meshtastic/tcp_transport.py's module docstring for the
    # full state machine these map onto. Not reused by Serial/BLE: Serial
    # has no networking layer to fail at this granularity, and BLE has no
    # DNS/refused-connection concept - a slow/failed BLE GATT connect is
    # already covered by the generic CONNECT_FAILED/TIMEOUT path there.
    DNS_ERROR = "dns_error"
    CONNECT_REFUSED = "connect_refused"
    CONNECT_TIMEOUT = "connect_timeout"
    # The raw TCP socket connected successfully (proven either by
    # TCPTransport's own pre-flight probe or by the meshtastic library's
    # own internal connect succeeding) but the Meshtastic protocol layer
    # then raised an error IMMEDIATELY - a fast, synchronous rejection,
    # not a hang. Distinct from PROTOCOL_SYNC_TIMEOUT (below), which
    # means the handshake never returned at all within the caller's
    # budget and had to be reported by TCPTransport's own external
    # timeout watchdog instead of a raised exception. A caller/UI can
    # tell "the host:port is reachable but immediately rejected the
    # Meshtastic handshake" (this code - e.g. something other than a
    # Meshtastic radio is listening on that port) apart from "the radio
    # accepted the TCP connection but the handshake never completes"
    # (PROTOCOL_SYNC_TIMEOUT - the exact regression-firmware shape this
    # feature was built to diagnose cleanly instead of hanging on).
    TCP_CONNECTED = "tcp_connected"
    # The Meshtastic protocol handshake was already in progress
    # (TCPTransport's own internal state was SYNCING - see that module's
    # state machine) when the caller's overall timeout budget ran out,
    # per tier 1 of the RadioTransport timeout contract (watched from
    # OUTSIDE the blocking library call - see
    # adapters/meshtastic/_timeout_support.py, the same mechanism every
    # other TransportError(TIMEOUT) in this codebase already relies on).
    # This is the code a firmware that accepts the TCP connection, sends
    # some FromRadio traffic, and then never reaches config_complete is
    # expected to produce - a self-reported sync timeout, reported
    # cleanly, never left as an indefinite hang and never collapsed into
    # the generic TIMEOUT code that doesn't say which stage got stuck.
    PROTOCOL_SYNC_TIMEOUT = "protocol_sync_timeout"
    # A previously READY (fully synced) TCP link stopped working during
    # normal use - a send_*/get_* call hit a socket-level error
    # (connection reset, broken pipe, EOF) rather than a connect()/
    # reconnect() attempt itself failing outright. Distinct from
    # CONNECT_FAILED/TIMEOUT (both mean "a connect attempt itself never
    # succeeded") - this means a link that WAS working stopped being
    # usable, which is what actually drives TCPTransport's own reconnect
    # backoff rather than requiring a caller-initiated reconnect() call.
    REMOTE_DISCONNECT = "remote_disconnect"


# ---------------------------------------------------------------------------
# Neutral models — plain data, no protobuf/library types anywhere below.
# ---------------------------------------------------------------------------

@dataclass
class TransportError(Exception):
    """NOT frozen (Task 48 follow-up, live-caught): exception objects are
    inherently mutable in a couple of places by Python's own machinery
    (__traceback__, __cause__/__context__ via `raise ... from ...`) -
    freezing this was a mismatch with that contract from the moment this
    class was introduced (Task 43.5), just never triggered until a real
    TransportError propagated out of a generator-based @contextmanager
    (claim_for_external_command()/_claim_radio()) for the first time:
    contextlib's _GeneratorContextManager.__exit__ does
    `exc.__traceback__ = traceback` when letting an exception through
    unchanged, which a frozen dataclass's __setattr__ rejects outright -
    dataclasses.FrozenInstanceError, masking the real underlying error.
    Reproduced in isolation and confirmed fixed by dropping frozen=True
    before this change landed; see
    tests/test_adapter_ipc_client.py::test_transport_error_raised_inside_claim_propagates_cleanly_not_frozeninstanceerror
    for the real-path regression test (not just the isolated repro).

    Losing frozen=True's hashability (eq=True + not frozen -> __hash__ is
    None) is fine here - checked before this change: nothing in this
    codebase uses a TransportError instance as a dict key or set member.
    """

    code: TransportErrorCode
    message: str

    def __str__(self) -> str:
        # dataclass's generated __init__ never calls Exception.__init__()
        # with (code, message) - but BaseException.__new__ still captures
        # the constructor's positional args into self.args, so the
        # inherited __str__ formats *that* tuple, repr()-ing the enum in
        # the process: "(<TransportErrorCode.TIMEOUT: 'timeout'>, '...')".
        # Not a crash, but useless in logs (caught live in prod's Task 44
        # verification: "[TIME SYNC] Attempt failed: (<TransportErrorCode..."
        # instead of a readable message) - overriding __str__ explicitly
        # is the fix, per review discussion.
        return f"{self.code.value}: {self.message}"


@dataclass(frozen=True)
class ConnectionDescriptor:
    """What to connect to. `address` is /dev/ttyACM0 for serial, a BLE MAC
    for bluetooth, host:port for tcp."""
    type: ConnectionType
    address: str
    label: str = ""


@dataclass(frozen=True)
class ConnectionInfo:
    state: ConnectionState
    descriptor: Optional[ConnectionDescriptor]
    node_id: Optional[str]
    connected_since: Optional[float] = None
    last_error: Optional[TransportError] = None


@dataclass(frozen=True)
class ConnectionEvent:
    """Point-in-time state transition, for callers that poll
    get_connection_info() (Stage A - see docs/BACKEND_API.md 'Events')."""
    state: ConnectionState
    descriptor: Optional[ConnectionDescriptor]
    detail: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass(frozen=True)
class NodeUser:
    id: str
    long_name: str
    short_name: str
    hw_model: str
    is_licensed: bool = False


@dataclass(frozen=True)
class NodeInfo:
    node_id: str
    num: int
    user: Optional[NodeUser]
    last_heard: Optional[float] = None
    snr: Optional[float] = None
    rssi: Optional[float] = None
    hop_count: Optional[int] = None
    is_favorite: bool = False
    device_metrics: dict = field(default_factory=dict)
    environment_metrics: dict = field(default_factory=dict)
    power_metrics: dict = field(default_factory=dict)
    position: Optional[dict] = None


@dataclass(frozen=True)
class ChannelInfo:
    index: int
    name: str
    role: str  # "PRIMARY" | "SECONDARY" | "DISABLED"


@dataclass(frozen=True)
class OutgoingMessage:
    text: str
    destination_id: str  # "^all" or "!xxxxxxxx"
    channel_index: int = 0
    want_ack: bool = False
    reply_id: Optional[int] = None


@dataclass(frozen=True)
class SendResult:
    accepted: bool
    packet_id: Optional[int] = None
    error: Optional[TransportError] = None


@dataclass(frozen=True)
class CheckedSendResult:
    """A send whose requested channel was resolved against the live radio
    in the same atomic transport session (see `send_text_checked`) - carries
    the ordinary `SendResult` plus the radio-reported name of the channel the
    message actually went out on (name only, never a PSK/secret)."""
    result: SendResult
    channel_name: str


@dataclass(frozen=True)
class OutgoingWaypoint:
    name: str
    description: str
    latitude: float
    longitude: float
    expire_at: int
    icon: int = 128205
    waypoint_id: Optional[int] = None
    channel_index: int = 0
    post_notification: bool = True
    notification_text: str = ""


@dataclass(frozen=True)
class WaypointResult:
    waypoint_id: int
    waypoint_packet_id: Optional[int] = None
    notification_packet_id: Optional[int] = None


@dataclass(frozen=True)
class TelemetryEvent:
    node_id: str
    kind: str  # "device" | "environment" | "power"
    metrics: dict
    timestamp: float


# ---------------------------------------------------------------------------
# Received events (inbound traffic from the radio)
# ---------------------------------------------------------------------------
#
# What a transport hands Core for one packet it received - deliberately NOT
# the library's packet dict or protobuf. The real packet carries a protobuf
# `raw` MeshPacket, `decoded.payload` bytes and (for waypoints) a second `raw`
# string; none of that may cross the IPC boundary, be logged, or reach Core's
# storage. So these are frozen, plain-typed, validated at construction, and the
# ONLY way onto the wire is the explicit per-field functions in
# meshsrv/ipc_protocol.py.
#
# `from_node_id` / `sender_id` / `to_node_id` are "!xxxxxxxx" node ids (or "^all"
# for a broadcast recipient), built by the transport from the packet's NUMERIC
# `from` / `to` (also carried as `from_num` / `to_num`) - never from the
# library's `fromId`, which is None whenever the sender is not yet in the local
# NodeDB (observed on meshtastic 2.7.9-2.7.11). `rx_time` is the radio's own
# clock; `received_at` is when the adapter saw the packet (time.time()).
#
# `local_radio_node_id` is the radio THIS event came from ("!xxxxxxxx" of the
# connected node). It is the safety field: before persisting anything Core
# compares it with the active accepted profile's node id and drops a mismatch,
# so events buffered from radio A can never be written into radio B's profile.
# An event without it cannot be constructed.

def _field_str(owner: str, name: str, value) -> str:
    if type(value) is not str:
        raise TypeError(f"{owner}.{name} must be str, got {type(value).__name__}")
    return value


def _field_nonempty_str(owner: str, name: str, value) -> str:
    _field_str(owner, name, value)
    if not value:
        raise ValueError(f"{owner}.{name} must not be empty")
    return value


def _field_int(owner: str, name: str, value) -> int:
    if type(value) is not int:  # bool is an int subclass - reject it too
        raise TypeError(f"{owner}.{name} must be int, got {type(value).__name__}")
    return value


def _field_optional_int(owner: str, name: str, value) -> Optional[int]:
    return None if value is None else _field_int(owner, name, value)


def _field_float(owner: str, name: str, value) -> float:
    if type(value) not in (int, float):
        raise TypeError(f"{owner}.{name} must be a number, got {type(value).__name__}")
    return float(value)


def _field_optional_float(owner: str, name: str, value) -> Optional[float]:
    return None if value is None else _field_float(owner, name, value)


def _field_optional_bool(owner: str, name: str, value) -> Optional[bool]:
    if value is not None and type(value) is not bool:
        raise TypeError(f"{owner}.{name} must be bool, got {type(value).__name__}")
    return value


def _field_metrics(owner: str, name: str, value) -> dict:
    """A flat {str: int|float} dict (one Telemetry variant's fields) - the one
    place a received event carries more than a handful of fixed columns.
    Still no nesting, no bytes, no protobuf: every key is a plain str, every
    value a plain int/float (bool rejected, same as everywhere else)."""
    if type(value) is not dict:
        raise TypeError(f"{owner}.{name} must be dict, got {type(value).__name__}")
    for key, metric in value.items():
        if type(key) is not str:
            raise TypeError(f"{owner}.{name} has a non-str key: {key!r}")
        if type(metric) not in (int, float):
            raise TypeError(f"{owner}.{name}[{key!r}] must be a number, got {type(metric).__name__}")
    return dict(value)


@dataclass(frozen=True)
class ReceivedTextEvent:
    from_node_id: str
    to_node_id: str  # "!xxxxxxxx" (direct message) or "^all" (broadcast on the channel)
    text: str
    received_at: float
    local_radio_node_id: str
    packet_id: Optional[int] = None
    from_num: Optional[int] = None
    to_num: Optional[int] = None
    channel_index: Optional[int] = None
    reply_id: Optional[int] = None
    rx_time: Optional[int] = None
    rx_rssi: Optional[int] = None
    rx_snr: Optional[float] = None
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    relay_node: Optional[int] = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _field_str(owner, "from_node_id", self.from_node_id)
        _field_str(owner, "to_node_id", self.to_node_id)
        _field_str(owner, "text", self.text)
        object.__setattr__(self, "received_at", _field_float(owner, "received_at", self.received_at))
        _field_nonempty_str(owner, "local_radio_node_id", self.local_radio_node_id)
        for name in ("packet_id", "from_num", "to_num", "channel_index", "reply_id", "rx_time",
                     "rx_rssi", "hop_limit", "hop_start", "relay_node"):
            _field_optional_int(owner, name, getattr(self, name))
        object.__setattr__(self, "rx_snr", _field_optional_float(owner, "rx_snr", self.rx_snr))


@dataclass(frozen=True)
class ReceivedWaypointEvent:
    waypoint_id: int
    sender_id: str
    name: str
    description: str
    received_at: float
    local_radio_node_id: str
    packet_id: Optional[int] = None
    latitude: Optional[float] = None  # degrees (the packet carries latitudeI / 1e7)
    longitude: Optional[float] = None
    icon: Optional[int] = None
    expire_at: Optional[int] = None
    channel_index: Optional[int] = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _field_int(owner, "waypoint_id", self.waypoint_id)
        _field_str(owner, "sender_id", self.sender_id)
        _field_str(owner, "name", self.name)
        _field_str(owner, "description", self.description)
        object.__setattr__(self, "received_at", _field_float(owner, "received_at", self.received_at))
        _field_nonempty_str(owner, "local_radio_node_id", self.local_radio_node_id)
        for name in ("packet_id", "icon", "expire_at", "channel_index"):
            _field_optional_int(owner, name, getattr(self, name))
        object.__setattr__(self, "latitude", _field_optional_float(owner, "latitude", self.latitude))
        object.__setattr__(self, "longitude", _field_optional_float(owner, "longitude", self.longitude))


@dataclass(frozen=True)
class ReceivedNodeInfoEvent:
    """One User/NODEINFO_APP packet - a node announcing its own identity.
    `node_id` is the User payload's own id (the node this info is ABOUT);
    `sender_id` is who the packet arrived from (built from the numeric `from`,
    same convention as ReceivedWaypointEvent.sender_id) - normally the same
    node, kept separate because they come from different parts of the packet
    and a mesh can in principle relay one for another.

    `hw_model` / `role` arrive from the library as enum NAME strings (e.g.
    "RAK4631", "ROUTER"), never the raw int - already true of the library's
    own MessageToDict output, not a conversion this event performs.
    `macaddr` / `public_key` are deliberately NOT carried: neither is needed
    by anything Core does with a NodeInfo today, and there is no reason to
    move a device's MAC or its crypto public key through IPC/storage/logs
    that doesn't already need them - a narrower default than the library's
    own dict, not an oversight (see the field-by-field serializer contract
    this whole module follows)."""

    node_id: str
    sender_id: str
    received_at: float
    local_radio_node_id: str
    packet_id: Optional[int] = None
    long_name: Optional[str] = None
    short_name: Optional[str] = None
    hw_model: Optional[str] = None
    role: Optional[str] = None
    is_licensed: Optional[bool] = None
    channel_index: Optional[int] = None
    rx_time: Optional[int] = None
    rx_rssi: Optional[int] = None
    rx_snr: Optional[float] = None
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    relay_node: Optional[int] = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _field_str(owner, "node_id", self.node_id)
        _field_str(owner, "sender_id", self.sender_id)
        object.__setattr__(self, "received_at", _field_float(owner, "received_at", self.received_at))
        _field_nonempty_str(owner, "local_radio_node_id", self.local_radio_node_id)
        for name in ("long_name", "short_name", "hw_model", "role"):
            value = getattr(self, name)
            if value is not None:
                _field_str(owner, name, value)
        object.__setattr__(self, "is_licensed", _field_optional_bool(owner, "is_licensed", self.is_licensed))
        for name in ("packet_id", "channel_index", "rx_time", "rx_rssi", "hop_limit", "hop_start", "relay_node"):
            _field_optional_int(owner, name, getattr(self, name))
        object.__setattr__(self, "rx_snr", _field_optional_float(owner, "rx_snr", self.rx_snr))


@dataclass(frozen=True)
class ReceivedPositionEvent:
    """One Position/POSITION_APP packet. `latitude`/`longitude` are plain
    degrees - the library's own _fixupPosition already converts latitudeI/
    longitudeI (1e-7 deg) to these before publishing, so (unlike Waypoint,
    whose serial/CLI path never gets that conversion for free) no manual /1e7
    division happens here; the adapter only reads the already-converted
    fields. `position_time` is the Position payload's own GPS/reported time,
    kept distinct from the packet's own `rx_time`."""

    sender_id: str
    received_at: float
    local_radio_node_id: str
    packet_id: Optional[int] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    altitude: Optional[int] = None
    ground_speed: Optional[int] = None
    sats_in_view: Optional[int] = None
    position_time: Optional[int] = None
    channel_index: Optional[int] = None
    rx_time: Optional[int] = None
    rx_rssi: Optional[int] = None
    rx_snr: Optional[float] = None
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    relay_node: Optional[int] = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _field_str(owner, "sender_id", self.sender_id)
        object.__setattr__(self, "received_at", _field_float(owner, "received_at", self.received_at))
        _field_nonempty_str(owner, "local_radio_node_id", self.local_radio_node_id)
        object.__setattr__(self, "latitude", _field_optional_float(owner, "latitude", self.latitude))
        object.__setattr__(self, "longitude", _field_optional_float(owner, "longitude", self.longitude))
        for name in ("packet_id", "altitude", "ground_speed", "sats_in_view", "position_time",
                     "channel_index", "rx_time", "rx_rssi", "hop_limit", "hop_start", "relay_node"):
            _field_optional_int(owner, name, getattr(self, name))
        object.__setattr__(self, "rx_snr", _field_optional_float(owner, "rx_snr", self.rx_snr))


TELEMETRY_KINDS = ("device", "environment", "power")


@dataclass(frozen=True)
class ReceivedTelemetryEvent:
    """One Telemetry/TELEMETRY_APP packet. Telemetry is a protobuf `oneof` -
    exactly one of device/environment/power metrics is present per packet -
    so, like the existing (Core-internal, polled) TelemetryEvent above, this
    carries `kind` + a flat `metrics` dict rather than one dataclass field per
    possible metric across all three variants (5 + 22 + 16 fields, almost
    always empty) - the exploded-field shape that fits Text/Waypoint/NodeInfo/
    Position does not fit telemetry's own shape. `metrics` is still validated
    (str keys, plain int/float values, no nesting) by the same discipline as
    every other field this module carries - see _field_metrics().
    `telemetry_time` is the Telemetry payload's own `time` field, distinct
    from the packet's `rx_time`."""

    sender_id: str
    kind: str  # "device" | "environment" | "power"
    metrics: dict
    received_at: float
    local_radio_node_id: str
    packet_id: Optional[int] = None
    telemetry_time: Optional[int] = None
    channel_index: Optional[int] = None
    rx_time: Optional[int] = None
    rx_rssi: Optional[int] = None
    rx_snr: Optional[float] = None
    hop_limit: Optional[int] = None
    hop_start: Optional[int] = None
    relay_node: Optional[int] = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        _field_str(owner, "sender_id", self.sender_id)
        if self.kind not in TELEMETRY_KINDS:
            raise ValueError(f"{owner}.kind must be one of {TELEMETRY_KINDS}, got {self.kind!r}")
        object.__setattr__(self, "metrics", _field_metrics(owner, "metrics", self.metrics))
        object.__setattr__(self, "received_at", _field_float(owner, "received_at", self.received_at))
        _field_nonempty_str(owner, "local_radio_node_id", self.local_radio_node_id)
        for name in ("packet_id", "telemetry_time", "channel_index", "rx_time", "rx_rssi",
                     "hop_limit", "hop_start", "relay_node"):
            _field_optional_int(owner, name, getattr(self, name))
        object.__setattr__(self, "rx_snr", _field_optional_float(owner, "rx_snr", self.rx_snr))


ReceivedEvent = Union[
    ReceivedTextEvent, ReceivedWaypointEvent, ReceivedNodeInfoEvent, ReceivedPositionEvent, ReceivedTelemetryEvent,
]


@dataclass(frozen=True)
class ReceivedBatch:
    """Result of one drain_received(): the events taken off the transport's
    buffer, plus what could not be delivered.

    `dropped`: events the transport's bounded buffer had to discard (oldest
    first) since the previous drain - overflow must be visible, never silent.
    `malformed`: events that arrived but could not be decoded on this side; each
    is discarded on its own, never the whole batch.
    `connection_generation`: bumps on every new physical connection/endpoint
    (observability only - node-id identity, not this, is the safety check)."""

    events: tuple = ()
    dropped: int = 0
    malformed: int = 0
    connection_generation: Optional[int] = None

    def __post_init__(self) -> None:
        owner = type(self).__name__
        object.__setattr__(self, "events", tuple(self.events))
        for event in self.events:
            if not isinstance(event, (
                ReceivedTextEvent, ReceivedWaypointEvent, ReceivedNodeInfoEvent,
                ReceivedPositionEvent, ReceivedTelemetryEvent,
            )):
                raise TypeError(f"{owner}.events holds a {type(event).__name__}, not a received event")
        _field_int(owner, "dropped", self.dropped)
        _field_int(owner, "malformed", self.malformed)
        _field_optional_int(owner, "connection_generation", self.connection_generation)


# ---------------------------------------------------------------------------
# The interface
# ---------------------------------------------------------------------------

class RadioTransport(abc.ABC):
    """Transport-neutral contract Core uses to talk to a Meshtastic radio.

    Concrete implementations (SerialTransport - Task 44, BLETransport -
    Task 45) live outside Core once the license-separation refactor lands
    (Task 48). Every method here returns only the models defined above -
    never a meshtastic.* / protobuf object.

    TIMEOUT CONTRACT (hardened by the live Task 43 BLE test on TAP2, where
    meshtastic's own BLEInterface connect hung well past its documented
    internal 60s timeout - ~90s with no response, required an external
    `kill -9` to recover). This is a two-tier guarantee, and the tiers are
    NOT equivalent - callers must not assume the stronger one applies
    before Task 48:

    1. NON-BLOCKING RETURN (mandatory from Task 44/45 onward): every method
       that accepts `timeout` MUST return or raise TransportError(TIMEOUT)
       to its caller at or before that many seconds have elapsed, enforced
       from OUTSIDE the underlying library call (e.g. `future.result(
       timeout=...)` on a call running in a watchdog thread). The wrapped
       library's own internal timeout is demonstrated-insufficient and must
       never be the only enforcement Core relies on for "did this hang".

    2. RESOURCE RELEASE (only guaranteed from Task 48 onward, when the
       adapter is an isolated subprocess and a timeout can SIGKILL it): a
       CPython thread cannot be force-terminated - only joined-with-timeout.
       So before Task 48, when tier 1 fires on e.g. a stuck BLEClient
       connect, the orphaned background thread (its own asyncio event loop,
       its live bleak/GATT session) keeps running unsupervised; it is
       *not* killed, only abandoned by the caller. This mirrors the exact
       failure this contract is named after: a leftover OS-level BLE
       session from one attempt blocking the next `connect()` until an
       out-of-band `bluetoothctl disconnect`. An implementation that needs
       real resource release before Task 48 must get it itself (e.g. by
       running the risky library call in its own short-lived subprocess
       rather than a thread, so it has something it actually can SIGKILL) -
       that is an implementation choice for Task 44/45, not something this
       interface can provide on their behalf.

    RECONNECT / TEARDOWN CONTRACT (also from the live Task 43 test - a
    stale OS-level BLE bond/GATT session left connected from a previous
    attempt silently blocked a fresh `--info` connect until explicitly
    disconnected at the OS level; separately, a live USB-serial listener
    had to be fully stopped before a BLE connect to the *same* node would
    succeed at all): `connect()` MUST NOT assume the radio or the local
    Bluetooth/serial stack is in a clean state. Implementations are
    responsible for tearing down any lower-layer connection they know
    about (OS bluez session, held serial fd, previous listener subprocess)
    before attempting a new connection when `force=True`. `disconnect()`
    and `close()` MUST NOT return until that teardown has actually
    completed - not just been requested - so a caller switching transports
    (e.g. Serial to BLE on the same node) can safely instantiate the next
    transport immediately after `close()` returns, without an extra
    out-of-band wait.

    This guarantee itself is subject to the timeout contract's tier-1/
    tier-2 split above: it holds whenever disconnect()/close() completes
    within its own `timeout`. If disconnect()/close() itself times out
    (tier 1 fires), the caller gets TransportError(TIMEOUT) back promptly,
    but - before Task 48 - there is no guarantee the lower-layer session
    was actually released; a subsequent connect(force=True) may still fail
    against a stack that thinks it's already connected, same as the live
    Task 43 finding. There is no interface-level fix for this before
    process isolation exists; it is a known, named gap, not an oversight.
    """

    @abc.abstractmethod
    def connect(
        self,
        descriptor: ConnectionDescriptor,
        *,
        force: bool = False,
        timeout: float = 30.0,
    ) -> ConnectionInfo:
        """Establish a connection. Idempotent if already connected to the
        same descriptor, unless force=True - then tear down any existing
        lower-layer session first (see class docstring) and reconnect."""

    @abc.abstractmethod
    def disconnect(self, *, timeout: float = 15.0) -> None:
        """Release the connection. Must fully complete before returning -
        see class docstring's reconnect/teardown contract."""

    @abc.abstractmethod
    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        """Equivalent to disconnect() followed by connect(<same descriptor>,
        force=True)."""

    @abc.abstractmethod
    def is_connected(self) -> bool:
        ...

    @abc.abstractmethod
    def send_text(
        self, message: OutgoingMessage, *, timeout: float = 15.0
    ) -> SendResult:
        ...

    @abc.abstractmethod
    def send_text_checked(
        self, message: OutgoingMessage, *, timeout: float = 15.0
    ) -> CheckedSendResult:
        """Send a text message AND resolve/validate `message.channel_index`
        against the live radio's channel list, all in ONE exclusive
        radio-interface session (acquire exclusive access once, open the
        interface once, wait-for-config once, read the channel list,
        validate the requested index, send, close once).

        Raises `TransportError(UNSUPPORTED)` if the requested channel index
        is not present (or is DISABLED) on the connected radio - the caller
        (MeshtasticTextAdapter) maps that to a fail-closed
        `ConnectorUnavailableError` rather than silently falling back to
        channel 0 (the public primary channel). This replaces the old
        two-call `get_channels()` + `send_text()` sequence, which claimed
        and opened the serial port twice per control message."""

    @abc.abstractmethod
    def send_packet(
        self,
        payload: bytes,
        destination_id: str,
        *,
        port_num: int,
        want_ack: bool = False,
        timeout: float = 15.0,
    ) -> SendResult:
        """Escape hatch for non-text application payloads. `payload` is
        already-serialized application data, never a protobuf object."""

    @abc.abstractmethod
    def send_messages(
        self, messages: Sequence[OutgoingMessage], *, timeout: float = 30.0
    ) -> list[SendResult]:
        """Send a batch over a single underlying connection - existing
        behavior in api/api_chat.py's send worker (_process_send_batch).
        Must not regress to one connect/disconnect cycle per message."""

    @abc.abstractmethod
    def send_waypoint(
        self, waypoint: OutgoingWaypoint, *, timeout: float = 15.0
    ) -> WaypointResult:
        ...

    @abc.abstractmethod
    def get_nodes(self, *, timeout: float = 15.0) -> list[NodeInfo]:
        ...

    @abc.abstractmethod
    def get_local_node(self, *, timeout: float = 15.0) -> NodeInfo:
        ...

    @abc.abstractmethod
    def get_channels(self, *, timeout: float = 15.0) -> list[ChannelInfo]:
        """Not in the original Task 43.5 operation list, but `channel` is
        already listed among the neutral models and api/api_chat.py's
        discover_radio_channels() reads exactly this today - without this
        method that existing functionality has nowhere to go in Task 44,
        the same gap the plan already called out for send_waypoint /
        set_device_time. Flagged for confirmation, not assumed silently."""

    @abc.abstractmethod
    def get_metadata(self, *, timeout: float = 15.0) -> dict:
        """Device metadata (firmware version, hwModel, capability flags) -
        same shape as `meshtastic --info`'s Metadata block, already reduced
        to primitives (plain dict, not a typed model - shape varies by
        firmware version, matches today's ad hoc handling)."""

    @abc.abstractmethod
    def set_device_time(self, epoch_seconds: int, *, timeout: float = 15.0) -> bool:
        ...

    @abc.abstractmethod
    def get_connection_info(self) -> ConnectionInfo:
        """Non-blocking - returns the last known state, does not itself
        talk to the radio. The polling substitute for a real event stream
        until Stage B (Task 49+) lands."""

    def drain_received(self, *, limit: int = 100, timeout: float = 5.0) -> ReceivedBatch:
        """Take (and remove) up to `limit` inbound events buffered since the
        last call, oldest first, as a ReceivedBatch (an empty batch when
        nothing is pending). Reads the transport's in-memory buffer only - it
        never talks to the radio. `timeout` bounds how long the call may wait
        for the transport to answer.

        OPTIONAL, deliberately not abstract: only a transport that can
        actually receive implements it (TCP first, then Bluetooth). The default
        raises UNSUPPORTED, so Serial - whose inbound traffic still arrives
        through Core's own `--listen` subprocess - and every existing test
        double keep working unchanged, and a caller can tell "cannot receive"
        from "nothing received" (an empty batch)."""
        raise TransportError(
            TransportErrorCode.UNSUPPORTED, f"{type(self).__name__} does not support receiving"
        )

    @abc.abstractmethod
    def close(self) -> None:
        """Final teardown. Same completion guarantee as disconnect() - see
        class docstring."""
