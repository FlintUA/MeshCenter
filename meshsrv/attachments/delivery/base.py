"""meshsrv/attachments/delivery/base.py

Transport-neutral DeliveryAdapter contract (design spec section 5.4;
ADR-0001 section 2). This module and everything else under
meshsrv/attachments/delivery/ is MIT-licensed Core code: no module here may
import the `meshtastic` Python package, its protobufs, or anything from
adapters/meshtastic/ - see docs/architecture/ADR-0001-mca-protocol.md and
CLAUDE.md's "GPLv3 process isolation" section.

The existing `RadioTransport`/`TransportRouter` (meshsrv/) handles the
physical connection to radio hardware. `DeliveryAdapter` sits above it and
is responsible only for the *meaning* of delivering one MCA logical
message: choosing a route, turning `logical_message` bytes (canonical CBOR
produced by meshsrv.attachments.codec) into a transport-specific wire
payload, sending it, and recovering a `DeliveryEnvelope` from an inbound
transport event. These two layers are never merged: MeshCore and
Meshtastic use different companion protocols, and Telegram/WhatsApp have
no radio transport at all.
"""

from __future__ import annotations

import abc
import dataclasses
import enum
from typing import Any, FrozenSet, Mapping, Optional


class WireFormat(enum.Enum):
    """The two wire encodings defined by ADR-0001 section 2."""

    MCA1_TEXT = "MCA1_TEXT"
    MCA1_CBOR = "MCA1_CBOR"


class RouteType(enum.Enum):
    DIRECT = "DIRECT"
    CHANNEL = "CHANNEL"
    CHAT = "CHAT"
    MANUAL = "MANUAL"


class ConnectorState(enum.Enum):
    """Coarse health of the underlying connector, as reported by
    `capabilities()`. Core uses this to decide whether an adapter should be
    offered as a send target at all - it is not a substitute for the
    per-call error raised by `send()`."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class AckSemantics(enum.Enum):
    """What kind of delivery confirmation this adapter's transport can give.

    BLE Meshtastic connections (design spec section 19.3) are the reason
    this exists as its own enum rather than a bool: MVP UI must show
    "Отправка без подтверждения" whenever the active transport reports
    BEST_EFFORT, rather than assuming every adapter behaves like USB serial.
    """

    NONE = "NONE"
    BEST_EFFORT = "BEST_EFFORT"
    CONFIRMED = "CONFIRMED"


class DeliveryError(RuntimeError):
    """Base class for every error an adapter raises instead of silently
    truncating, dropping, or falling back. `encode()`/`send()` must raise
    (a subclass of) this rather than returning a partial/invalid payload."""


class PayloadTooLargeError(DeliveryError):
    """Raised by `encode()` when the wire payload for `logical_message`
    exceeds this adapter's own `capabilities().max_payload_bytes`. The
    check happens against the adapter's own encoded output, never against
    a global constant borrowed from another transport."""

    def __init__(self, encoded_bytes: int, limit_bytes: int):
        super().__init__(
            f"encoded payload is {encoded_bytes} bytes, "
            f"adapter limit is {limit_bytes} bytes"
        )
        self.encoded_bytes = encoded_bytes
        self.limit_bytes = limit_bytes


class UnsupportedRouteError(DeliveryError):
    """Raised by `resolve_route()`/`encode()` when the requested route type
    or selection is not one this adapter supports (e.g. asking a
    direct-only adapter to resolve a CHANNEL route)."""


class ConnectorUnavailableError(DeliveryError):
    """Raised by `send()` when the underlying connector cannot currently
    send at all (e.g. serial port not claimed, no radio attached)."""


@dataclasses.dataclass(frozen=True)
class Route:
    """Resolved destination for one send, independent of wire_format."""

    route_type: RouteType
    route_id: str
    destination_address: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class DeliveryCapabilities:
    """What one adapter instance can currently do. Core queries this before
    offering the adapter as a send target, but the authoritative size limit
    for any one message is always re-checked against that message's actual
    `encode()` output (see PayloadTooLargeError), never read out of this
    struct and assumed to hold for every message.
    """

    wire_formats: FrozenSet[WireFormat]
    max_payload_bytes: int
    supports_direct: bool
    supports_channel: bool
    supports_incoming: bool
    ack_semantics: AckSemantics
    connector_state: ConnectorState


@dataclasses.dataclass(frozen=True)
class DeliveryReceipt:
    """Result of one `send()` call."""

    sent: bool
    idempotency_key: str
    external_message_id: Optional[str] = None
    sent_at: Optional[float] = None


@dataclasses.dataclass(frozen=True)
class DeliveryEnvelope:
    """Recovered from an inbound transport event by `ingest()`.

    `logical_message` is the canonical CBOR bytes (ADR-0001 section 2) -
    already stripped of the transport envelope and, for MCA1_TEXT, already
    Base64URL-decoded. It has NOT necessarily had its signature verified;
    that happens one layer up, in the codec/state-machine, once the
    signer's public key is known.
    """

    logical_message: bytes
    wire_format: WireFormat
    adapter_id: str
    connector_profile_id: str
    route_type: RouteType
    route_id: str
    source_address: Optional[str]
    external_message_id: Optional[str]
    received_at: float
    transport_metadata: Mapping[str, Any] = dataclasses.field(default_factory=dict)


class DeliveryAdapter(abc.ABC):
    """Transport-neutral contract every real adapter (Meshtastic, MeshCore,
    Telegram, WhatsApp, manual/clipboard, and the fake adapters used in
    Stage 0 contract tests) must implement.

    Core never imports a transport-specific type through this interface -
    everything crossing the boundary is either a primitive, one of the
    dataclasses/enums above, or opaque `bytes`.
    """

    adapter_id: str

    @abc.abstractmethod
    def capabilities(self) -> DeliveryCapabilities:
        """Return this adapter instance's current capabilities. May change
        over the adapter's lifetime (e.g. connector_state flipping to
        UNAVAILABLE when a serial port is unplugged) - Core must not cache
        this indefinitely."""

    @abc.abstractmethod
    def resolve_route(self, user_selection: Mapping[str, Any]) -> Route:
        """Turn a UI-level selection (contact, channel, chat id, ...) into
        a concrete `Route`. Must raise `UnsupportedRouteError` rather than
        guess or silently fall back to a different route type/privacy
        level than the user selected."""

    @abc.abstractmethod
    def encode(self, logical_message: bytes, route: Route) -> bytes:
        """Encode `logical_message` (canonical CBOR from
        meshsrv.attachments.codec) into this adapter's wire payload for
        `route`. Must be deterministic (same inputs -> identical bytes)
        and must raise `PayloadTooLargeError` (checked against the actual
        encoded output, not a global constant) rather than truncate."""

    @abc.abstractmethod
    def send(self, wire_payload: bytes, route: Route, idempotency_key: str) -> DeliveryReceipt:
        """Send an already-encoded payload. Must not re-encode or mutate
        `wire_payload`. Repeated calls with the same `idempotency_key` must
        not cause duplicate delivery on transports where that is possible
        to guarantee."""

    @abc.abstractmethod
    def ingest(self, transport_event: Any) -> Optional[DeliveryEnvelope]:
        """Inspect one raw transport event and, if it looks like an MCA
        message for this adapter, return the recovered `DeliveryEnvelope`.
        Return `None` ("ignored") for anything else - ingest must never
        raise merely because an event is not MCA; only malformed events
        that *do* claim to be MCA (e.g. bad prefix after being routed here
        as such) should raise a `DeliveryError`."""
