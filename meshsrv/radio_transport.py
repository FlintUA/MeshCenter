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
from typing import Optional, Sequence

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

    @abc.abstractmethod
    def close(self) -> None:
        """Final teardown. Same completion guarantee as disconnect() - see
        class docstring."""
