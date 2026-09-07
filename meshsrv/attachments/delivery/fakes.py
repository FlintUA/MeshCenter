"""meshsrv/attachments/delivery/fakes.py

Two fake DeliveryAdapter implementations used only by tests
(tests/test_delivery_contract.py, Execution Plan Step 0.4). They exist to
prove Core can drive a text-limited transport (like Meshtastic, 180-byte
ceiling) and a binary-limited transport (like MeshCore, 163-byte ceiling)
through the exact same contract, without importing anything transport
specific - see meshsrv/attachments/delivery/base.py.

Both adapters share a synchronous in-memory "ether": `send()` on one
instance immediately makes the transport event available to any other
instance registered at the destination address, so tests don't need real
sockets, threads, or time to pass.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from meshsrv.attachments import codec
from meshsrv.attachments.delivery.base import (
    AckSemantics,
    ConnectorState,
    DeliveryAdapter,
    DeliveryCapabilities,
    DeliveryEnvelope,
    DeliveryError,
    DeliveryReceipt,
    PayloadTooLargeError,
    Route,
    RouteType,
    UnsupportedRouteError,
    WireFormat,
)
from meshsrv.radio_transport import (
    ChannelInfo,
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    OutgoingMessage,
    RadioTransport,
    SendResult,
    WaypointResult,
)


class InMemoryEther:
    """Shared synchronous transport for fake adapters in tests.

    Not part of the DeliveryAdapter contract itself - it is test
    infrastructure standing in for "the mesh"/"the messenger API", so real
    adapters have no equivalent of this class.
    """

    def __init__(self) -> None:
        self._inboxes: Dict[str, List[Any]] = {}

    def register(self, address: str) -> None:
        self._inboxes.setdefault(address, [])

    def deliver(self, to_address: str, event: Any) -> None:
        self._inboxes.setdefault(to_address, []).append(event)

    def drain(self, address: str) -> List[Any]:
        inbox = self._inboxes.setdefault(address, [])
        events, inbox[:] = list(inbox), []
        return events


class FakeTextAdapter(DeliveryAdapter):
    """Emulates a text-capable transport with a Meshtastic-like 180-byte
    application budget for `MCA1-TEXT` messages."""

    adapter_id = "fake-text"

    def __init__(self, ether: InMemoryEther, own_address: str, max_payload_bytes: int = 180):
        self._ether = ether
        self._own_address = own_address
        self._max_payload_bytes = max_payload_bytes
        ether.register(own_address)

    @property
    def connector_profile_id(self) -> str:
        """PR #231 review (3rd pass): exposed publicly so tests can
        reference the same value `ingest()` already reports in its
        `DeliveryEnvelope` (`connector_profile_id=self._own_address`
        below) when building a `receiver.ReplyRoute` for a strict
        adapter/connector-validation test, rather than reaching into the
        private `_own_address` attribute directly."""
        return self._own_address

    def capabilities(self) -> DeliveryCapabilities:
        return DeliveryCapabilities(
            wire_formats=frozenset({WireFormat.MCA1_TEXT}),
            max_payload_bytes=self._max_payload_bytes,
            supports_direct=True,
            supports_channel=False,
            supports_incoming=True,
            ack_semantics=AckSemantics.BEST_EFFORT,
            connector_state=ConnectorState.READY,
        )

    def resolve_route(self, user_selection: Mapping[str, Any]) -> Route:
        address = user_selection.get("address")
        if not address:
            raise UnsupportedRouteError("fake-text requires user_selection['address']")
        return Route(route_type=RouteType.DIRECT, route_id=str(address), destination_address=str(address))

    def encode(self, logical_message: bytes, route: Route) -> bytes:
        if route.route_type != RouteType.DIRECT:
            raise UnsupportedRouteError(f"fake-text only supports DIRECT routes, got {route.route_type}")
        text = codec.to_text(logical_message)
        wire_payload = text.encode("ascii")
        if len(wire_payload) > self._max_payload_bytes:
            raise PayloadTooLargeError(len(wire_payload), self._max_payload_bytes)
        return wire_payload

    def send(self, wire_payload: bytes, route: Route, idempotency_key: str) -> DeliveryReceipt:
        if route.destination_address is None:
            raise UnsupportedRouteError("fake-text route has no destination_address")
        event = {
            "text": wire_payload.decode("ascii"),
            "from": self._own_address,
            "idempotency_key": idempotency_key,
        }
        self._ether.deliver(route.destination_address, event)
        return DeliveryReceipt(
            sent=True,
            idempotency_key=idempotency_key,
            external_message_id=idempotency_key,
            sent_at=time.time(),
        )

    def ingest(self, transport_event: Any) -> Optional[DeliveryEnvelope]:
        if not isinstance(transport_event, dict) or "text" not in transport_event:
            return None
        text = transport_event["text"]
        if not isinstance(text, str) or not text.startswith(codec.TEXT_PREFIX):
            return None
        try:
            logical_message = codec.from_text(text)
        except codec.CodecError as exc:
            raise DeliveryError(f"fake-text ingest: malformed MCA1-TEXT payload: {exc}") from exc
        return DeliveryEnvelope(
            logical_message=logical_message,
            wire_format=WireFormat.MCA1_TEXT,
            adapter_id=self.adapter_id,
            connector_profile_id=self._own_address,
            route_type=RouteType.DIRECT,
            route_id=str(transport_event.get("from", "")),
            source_address=transport_event.get("from"),
            external_message_id=transport_event.get("idempotency_key"),
            received_at=time.time(),
            transport_metadata={},
        )


class FakeBinaryAdapter(DeliveryAdapter):
    """Emulates a binary-channel transport with a MeshCore-like 163-byte
    datagram budget for `MCA1-CBOR` messages."""

    adapter_id = "fake-binary"

    def __init__(self, ether: InMemoryEther, own_address: str, max_payload_bytes: int = 163):
        self._ether = ether
        self._own_address = own_address
        self._max_payload_bytes = max_payload_bytes
        ether.register(own_address)

    def capabilities(self) -> DeliveryCapabilities:
        return DeliveryCapabilities(
            wire_formats=frozenset({WireFormat.MCA1_CBOR}),
            max_payload_bytes=self._max_payload_bytes,
            supports_direct=True,
            supports_channel=True,
            supports_incoming=True,
            ack_semantics=AckSemantics.CONFIRMED,
            connector_state=ConnectorState.READY,
        )

    def resolve_route(self, user_selection: Mapping[str, Any]) -> Route:
        address = user_selection.get("address")
        if not address:
            raise UnsupportedRouteError("fake-binary requires user_selection['address']")
        route_type = RouteType.CHANNEL if user_selection.get("channel") else RouteType.DIRECT
        return Route(route_type=route_type, route_id=str(address), destination_address=str(address))

    def encode(self, logical_message: bytes, route: Route) -> bytes:
        wire_payload = codec.to_cbor(logical_message)
        if len(wire_payload) > self._max_payload_bytes:
            raise PayloadTooLargeError(len(wire_payload), self._max_payload_bytes)
        return wire_payload

    def send(self, wire_payload: bytes, route: Route, idempotency_key: str) -> DeliveryReceipt:
        if route.destination_address is None:
            raise UnsupportedRouteError("fake-binary route has no destination_address")
        event = {
            "bytes": bytes(wire_payload),
            "from": self._own_address,
            "route_type": route.route_type,
            "idempotency_key": idempotency_key,
        }
        self._ether.deliver(route.destination_address, event)
        return DeliveryReceipt(
            sent=True,
            idempotency_key=idempotency_key,
            external_message_id=idempotency_key,
            sent_at=time.time(),
        )

    def ingest(self, transport_event: Any) -> Optional[DeliveryEnvelope]:
        if not isinstance(transport_event, dict) or "bytes" not in transport_event:
            return None
        raw = transport_event["bytes"]
        if not isinstance(raw, (bytes, bytearray)):
            return None
        try:
            logical_message = codec.from_cbor(bytes(raw))
            # from_cbor() is identity-plus-size-guard only (ADR-0001 section 2
            # carries no length prefix or framing of its own for MCA1-CBOR),
            # so it cannot by itself detect non-CBOR garbage. Force a real
            # decode here so ingest() rejects malformed payloads the same way
            # the text adapter's from_text()+Base64 check already does.
            codec.peek_message_type(logical_message)
        except codec.CodecError as exc:
            raise DeliveryError(f"fake-binary ingest: malformed MCA1-CBOR payload: {exc}") from exc
        route_type = transport_event.get("route_type", RouteType.DIRECT)
        return DeliveryEnvelope(
            logical_message=logical_message,
            wire_format=WireFormat.MCA1_CBOR,
            adapter_id=self.adapter_id,
            connector_profile_id=self._own_address,
            route_type=route_type,
            route_id=str(transport_event.get("from", "")),
            source_address=transport_event.get("from"),
            external_message_id=transport_event.get("idempotency_key"),
            received_at=time.time(),
            transport_metadata={},
        )


class FakeRadioTransport(RadioTransport):
    """In-memory `RadioTransport` stand-in for `MeshtasticTextAdapter`'s
    own contract tests (Execution Plan Step 1.3) - the *adapter* under
    test is real, only the radio underneath it is faked, the same
    division `FakeTextAdapter`/`FakeBinaryAdapter` above draw between
    "real adapter logic" and "fake ether".

    Only `send_text()` and `get_connection_info()`/`is_connected()` have
    real behavior - `MeshtasticTextAdapter` never calls anything else on
    a `RadioTransport`. Every other abstract method raises
    `NotImplementedError` outright rather than returning a plausible-
    looking fake value nothing here actually exercises - a test that
    somehow reached one of them would fail loudly instead of silently
    passing against made-up data.

    Delivers `{"text": ..., "source_address": ...}` into the shared
    `InMemoryEther` - already shaped exactly like the dict
    `MeshtasticTextAdapter.ingest()` expects (see server.py's real
    listener hook, which builds the same shape), so a test can drain the
    ether and hand the event straight to the receiver's `ingest()`
    without any adapter-specific translation step.
    """

    def __init__(
        self,
        ether: InMemoryEther,
        own_address: str,
        *,
        connection_type: ConnectionType = ConnectionType.SERIAL,
        connection_state: ConnectionState = ConnectionState.CONNECTED,
    ):
        self._ether = ether
        self._own_address = own_address
        self._connection_type = connection_type
        self._connection_state = connection_state
        self._next_packet_id = 1
        ether.register(own_address)

    def send_text(self, message: OutgoingMessage, *, timeout: float = 15.0) -> SendResult:
        packet_id = self._next_packet_id
        self._next_packet_id += 1
        self._ether.deliver(
            message.destination_id,
            {"text": message.text, "source_address": self._own_address, "packet_id": packet_id},
        )
        return SendResult(accepted=True, packet_id=packet_id)

    def get_connection_info(self) -> ConnectionInfo:
        return ConnectionInfo(
            state=self._connection_state,
            descriptor=ConnectionDescriptor(type=self._connection_type, address=self._own_address),
            node_id=self._own_address,
        )

    def is_connected(self) -> bool:
        return self._connection_state == ConnectionState.CONNECTED

    # ---- Not exercised by MeshtasticTextAdapter - see class docstring. ----

    def connect(self, descriptor, *, force: bool = False, timeout: float = 30.0) -> ConnectionInfo:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def disconnect(self, *, timeout: float = 15.0) -> None:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def reconnect(self, *, timeout: float = 30.0) -> ConnectionInfo:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def send_packet(self, payload, destination_id, *, port_num, want_ack=False, timeout=15.0):
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def send_messages(self, messages: Sequence[OutgoingMessage], *, timeout: float = 30.0) -> list:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def send_waypoint(self, waypoint, *, timeout: float = 15.0) -> WaypointResult:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def get_nodes(self, *, timeout: float = 15.0) -> List[NodeInfo]:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def get_local_node(self, *, timeout: float = 15.0) -> NodeInfo:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def get_channels(self, *, timeout: float = 15.0) -> List[ChannelInfo]:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def get_metadata(self, *, timeout: float = 15.0) -> dict:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def set_device_time(self, epoch_seconds: int, *, timeout: float = 15.0) -> bool:
        raise NotImplementedError("FakeRadioTransport is send/ingest-only - not exercised by these tests")

    def close(self) -> None:
        pass
