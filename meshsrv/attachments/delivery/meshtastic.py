"""meshsrv/attachments/delivery/meshtastic.py

Real `DeliveryAdapter` for Meshtastic direct text messages (Execution
Plan Step 1.3; design spec section 19). MIT-licensed Core code: imports
only `meshsrv.radio_transport`'s neutral models and
`meshsrv.attachments.*` - never `meshtastic`, never anything under
`adapters/meshtastic/`. `RadioTransport` (implemented by
`AdapterIPCTransport`, routed through `TransportRouter` - see
server.py's `transport_router` global and api/api_chat.py's
`radio_transport` DI parameter for the existing pattern this follows)
is injected at construction, exactly like every other consumer of the
transport layer; this module never constructs one itself.

Stage 1 scope only (spec 19, "MVP"): DIRECT routes over Meshtastic text
messages, `MCA1-TEXT` wire format. No CHANNEL route support (a KEY_REQUEST/
KEY_ANNOUNCE broadcast to a channel is explicitly out of scope per
key_exchange.py's own spec-7.4 comment) - `capabilities().supports_channel`
is `False` and `encode()`/`resolve_route()` refuse anything but
`RouteType.DIRECT`, same as `FakeTextAdapter` in tests/fakes.

The one channel-related knob this adapter DOES own is the send-time
*channel index* MCA control traffic is transmitted on
(`control_channel_index`, from config.py's `MCA_CONTROL_CHANNEL_INDEX`).
That is not a CHANNEL route (the destination is still a specific node,
`RouteType.DIRECT`) - it only selects which channel index the radio
transmits the DIRECT message on, the same selection a normal chat send
makes. Because channel 0 is the public primary channel, MCA control
traffic is meant to run on a private channel index; the configured index
is validated 0-7 and resolved against the live radio at send time, and an
invalid/unavailable value - or `None` (missing config, the default, which
has NO implicit fallback) - blocks transmission rather than silently
falling back to 0.

BLE ack semantics (spec 19.3): MeshCenter's existing, already-documented
limitation is that it cannot reliably *receive* over BLE at all (see
CLAUDE.md's "Known, accepted trade-offs" - BLE receive-blindness is
pre-existing, not introduced here). This adapter cannot fix that, but it
must not silently claim a confirmation guarantee it cannot back up:
`capabilities().ack_semantics` reports `BEST_EFFORT` whenever the
underlying transport's live `ConnectionDescriptor.type` is `BLUETOOTH`,
`CONFIRMED` over `SERIAL`. Callers building send-flow UI read this field
rather than assuming USB-serial-style confirmation universally.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping, Optional

from meshsrv.attachments import codec
from meshsrv.attachments.delivery.base import (
    AckSemantics,
    ConnectorState,
    ConnectorUnavailableError,
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
    ConnectionState,
    ConnectionType,
    OutgoingMessage,
    RadioTransport,
    TransportError,
)

# ADR-0001 section 4's own text-transport ceiling for MCA1-TEXT (OFFER and
# KEY_ANNOUNCE both budget against this same 180-byte number). Meshtastic's
# real per-message text application budget is documented at ~200 bytes
# (see ADR-0001's References section); 180 keeps headroom below that for
# the transport's own envelope overhead rather than targeting the ceiling
# exactly - this is the same number the fakes/tests already assume, not a
# new one invented here.
MESHTASTIC_TEXT_MAX_PAYLOAD_BYTES = 180

logger = logging.getLogger(__name__)


class MeshtasticTextAdapter(DeliveryAdapter):
    """Real Meshtastic `DeliveryAdapter`, driven through an injected
    `RadioTransport` (normally `server.py`'s shared `TransportRouter`
    instance - never constructed by this class)."""

    adapter_id = "meshtastic"

    # PR #231 review (3rd pass): the single, fixed connector_profile_id
    # this adapter has ever actually produced - ingest()'s own default
    # (`transport_event.get("connector_profile_id", "meshtastic")`)
    # already falls through to this exact literal, since no real or fake
    # call site anywhere in this codebase ever populates that key in the
    # transport_event dict it builds (server.py's listener, FakeRadioTransport.
    # send_text() - neither sets it). Exposed here as a real attribute
    # (not just an inline string inside ingest()) so AttachmentsService's
    # ACK-dispatch path can validate a persisted reply route's
    # connector_profile_id against the adapter it is actually about to
    # send through, the same way it already validates adapter_id.
    connector_profile_id = "meshtastic"

    def __init__(
        self,
        radio_transport: RadioTransport,
        *,
        max_payload_bytes: int = MESHTASTIC_TEXT_MAX_PAYLOAD_BYTES,
        control_channel_index: Optional[int] = None,
    ):
        self._radio_transport = radio_transport
        self._max_payload_bytes = max_payload_bytes
        # The Meshtastic channel *index* MCA DIRECT control traffic is
        # transmitted on. Distinct from the CHANNEL route type (broadcast to
        # a channel) - those remain unsupported (supports_channel=False):
        # this only selects which channel index carries an otherwise
        # ordinary DIRECT node-to-node message, exactly like a normal chat
        # send's own channel selection. `None` (the default) means "no
        # control channel has been configured" and MUST fail closed at send
        # time rather than silently defaulting to channel 0 (the public
        # primary channel) - see config.example.py's MCA_CONTROL_CHANNEL_INDEX.
        self._control_channel_index = control_channel_index

    def capabilities(self) -> DeliveryCapabilities:
        info = self._radio_transport.get_connection_info()

        if info.state == ConnectionState.CONNECTED:
            connector_state = ConnectorState.READY
        elif info.state == ConnectionState.CONNECTING:
            connector_state = ConnectorState.DEGRADED
        else:
            # DISCONNECTED or ERROR - either way, not currently usable as
            # a send target.
            connector_state = ConnectorState.UNAVAILABLE

        is_ble = info.descriptor is not None and info.descriptor.type == ConnectionType.BLUETOOTH
        # spec 19.3: BLE cannot reliably receive at all on current
        # MeshCenter (CLAUDE.md's own documented, pre-existing
        # receive-blindness trade-off) - BEST_EFFORT, never CONFIRMED,
        # whenever the live transport is BLE. USB serial is the only mode
        # this adapter's own hardware acceptance test exercises.
        ack_semantics = AckSemantics.BEST_EFFORT if is_ble else AckSemantics.CONFIRMED

        return DeliveryCapabilities(
            wire_formats=frozenset({WireFormat.MCA1_TEXT}),
            max_payload_bytes=self._max_payload_bytes,
            supports_direct=True,
            supports_channel=False,
            supports_incoming=True,
            ack_semantics=ack_semantics,
            connector_state=connector_state,
        )

    def resolve_route(self, user_selection: Mapping[str, Any]) -> Route:
        node_id = user_selection.get("node_id")
        if not node_id or not str(node_id).startswith("!"):
            raise UnsupportedRouteError(
                "meshtastic adapter requires user_selection['node_id'] as a '!xxxxxxxx' address"
            )
        return Route(route_type=RouteType.DIRECT, route_id=str(node_id), destination_address=str(node_id))

    def encode(self, logical_message: bytes, route: Route) -> bytes:
        if route.route_type != RouteType.DIRECT:
            raise UnsupportedRouteError(
                f"meshtastic adapter only supports DIRECT routes in Stage 1, got {route.route_type}"
            )
        text = codec.to_text(logical_message)
        wire_payload = text.encode("ascii")
        if len(wire_payload) > self._max_payload_bytes:
            raise PayloadTooLargeError(len(wire_payload), self._max_payload_bytes)
        return wire_payload

    def send(self, wire_payload: bytes, route: Route, idempotency_key: str) -> DeliveryReceipt:
        if route.destination_address is None:
            raise UnsupportedRouteError("meshtastic route has no destination_address")
        # The destination remains the specific node (RouteType.DIRECT) - the
        # control-channel index only selects which channel *index* carries it.
        index = self._control_channel_index
        if isinstance(index, bool) or not isinstance(index, int) or not (0 <= index <= 7):
            raise ConnectorUnavailableError(
                f"MCA control channel index {index!r} is invalid (must be an int 0-7)"
            )
        message = OutgoingMessage(
            text=wire_payload.decode("ascii"),
            destination_id=route.destination_address,
            channel_index=index,
        )
        try:
            # One exclusive radio-interface session: the transport acquires
            # exclusive serial access, opens the interface once, waits for
            # config, reads the channel list, validates the configured index,
            # resolves its safe name, sends, and closes - atomic at the
            # adapter boundary (no separate get_channels() call before this,
            # no stale cache, no time-based delay sync). Raises
            # TransportError(UNSUPPORTED) when the configured channel is
            # absent/DISABLED on the live radio.
            checked = self._radio_transport.send_text_checked(message, timeout=15.0)
        except TransportError as exc:
            raise ConnectorUnavailableError(f"cannot verify MCA control channel {index}: {exc}") from exc
        result = checked.result
        if not result.accepted:
            reason = str(result.error) if result.error else "send_text() did not accept the message"
            raise ConnectorUnavailableError(reason)
        # Log only the channel index and its radio-reported name, never any
        # channel PSK or other secret - ChannelInfo carries none to leak.
        logger.info(
            "MCAttach control message sent on channel %d (%s) to %s",
            index, checked.channel_name, route.destination_address,
        )
        return DeliveryReceipt(
            sent=True,
            idempotency_key=idempotency_key,
            external_message_id=str(result.packet_id) if result.packet_id is not None else None,
            sent_at=time.time(),
        )

    def ingest(self, transport_event: Any) -> Optional[DeliveryEnvelope]:
        """`transport_event` is a small, adapter-agnostic dict Core's own
        listener hook builds (see server.py's incoming-text dispatch,
        right after the normal message is saved) - not a Meshtastic/
        protobuf type, keeping this method's input shape identical to
        `FakeTextAdapter.ingest()`'s for the contract tests. Expected
        keys: `text` (str), `source_address` (the sender's `!node_id`),
        optionally `packet_id`, `received_at`, and `channel_index` (the
        channel index the message actually arrived on - retained in the
        envelope's `transport_metadata`, never used to accept/reject).
        """
        if not isinstance(transport_event, dict):
            return None
        text = transport_event.get("text")
        if not isinstance(text, str) or not text.startswith(codec.TEXT_PREFIX):
            return None
        try:
            logical_message = codec.from_text(text)
        except codec.CodecError as exc:
            raise DeliveryError(f"meshtastic ingest: malformed MCA1-TEXT payload: {exc}") from exc
        source_address = transport_event.get("source_address")
        # The received channel index (when the listener knows it) is retained
        # for observability only - inbound trust is signature/TOFU-based, so
        # arrival channel is NOT an accept/reject gate here (the channel is a
        # send-time selection; see AttachmentsService._process_one_inbound_event).
        received_channel_index = transport_event.get("channel_index")
        metadata: dict = {}
        if received_channel_index is not None:
            metadata["channel_index"] = received_channel_index
        return DeliveryEnvelope(
            logical_message=logical_message,
            wire_format=WireFormat.MCA1_TEXT,
            adapter_id=self.adapter_id,
            connector_profile_id=transport_event.get("connector_profile_id", self.connector_profile_id),
            route_type=RouteType.DIRECT,
            route_id=str(source_address) if source_address else "",
            source_address=source_address,
            external_message_id=transport_event.get("packet_id"),
            received_at=transport_event.get("received_at", time.time()),
            transport_metadata=metadata,
        )
