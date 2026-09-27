"""JSON wire (de)serialization for Task 48's subprocess IPC boundary -
the shape documented in docs/BACKEND_API.md's "JSON wire shape" section,
implemented here rather than redesigned. Shared, first-party, zero
third-party imports (like meshsrv/radio_transport.py itself) - both Core
and the adapter subprocess import this module directly, each under their
own venv, and both end up with the exact same Python types on either end
of the boundary (TransportError, ConnectionInfo, ...), not raw dicts -
see the Task 48 review discussion on why that matters (isinstance checks
and .code/.state enum access elsewhere in the codebase must keep working
unchanged).

Explicit per-type functions, not a generic dataclass-reflection walker -
easier to audit field-by-field on a new protocol boundary where a silent
mismatch would misroute or misinterpret data, matches this project's
existing preference for explicit mapping (e.g. api/api_meshtastic.py's
_connection_payload()) over generic serialization magic.
"""
from __future__ import annotations

from typing import Any, Optional

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
    ReceivedBatch,
    ReceivedEvent,
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

PROTOCOL_VERSION = 1


# ---------------------------------------------------------------------------
# TransportError
# ---------------------------------------------------------------------------
def error_to_dict(error: TransportError) -> dict:
    return {"code": error.code.value, "message": error.message}


def error_from_dict(data: Optional[dict]) -> Optional[TransportError]:
    if data is None:
        return None
    try:
        code = TransportErrorCode(data.get("code"))
    except ValueError:
        code = TransportErrorCode.UNKNOWN
    return TransportError(code, str(data.get("message", "")))


# ---------------------------------------------------------------------------
# ConnectionDescriptor
# ---------------------------------------------------------------------------
def descriptor_to_dict(descriptor: Optional[ConnectionDescriptor]) -> Optional[dict]:
    if descriptor is None:
        return None
    return {"type": descriptor.type.value, "address": descriptor.address, "label": descriptor.label}


def descriptor_from_dict(data: Optional[dict]) -> Optional[ConnectionDescriptor]:
    if data is None:
        return None
    return ConnectionDescriptor(
        type=ConnectionType(data.get("type")),
        address=str(data.get("address", "")),
        label=str(data.get("label", "")),
    )


# ---------------------------------------------------------------------------
# ConnectionInfo
# ---------------------------------------------------------------------------
def connection_info_to_dict(info: ConnectionInfo) -> dict:
    return {
        "state": info.state.value,
        "descriptor": descriptor_to_dict(info.descriptor),
        "node_id": info.node_id,
        "connected_since": info.connected_since,
        "last_error": error_to_dict(info.last_error) if info.last_error else None,
    }


def connection_info_from_dict(data: dict) -> ConnectionInfo:
    return ConnectionInfo(
        state=ConnectionState(data.get("state")),
        descriptor=descriptor_from_dict(data.get("descriptor")),
        node_id=data.get("node_id"),
        connected_since=data.get("connected_since"),
        last_error=error_from_dict(data.get("last_error")),
    )


# ---------------------------------------------------------------------------
# OutgoingMessage (Core -> adapter only, never a response type)
# ---------------------------------------------------------------------------
def outgoing_message_to_dict(message: OutgoingMessage) -> dict:
    return {
        "text": message.text,
        "destination_id": message.destination_id,
        "channel_index": message.channel_index,
        "want_ack": message.want_ack,
        "reply_id": message.reply_id,
    }


def outgoing_message_from_dict(data: dict) -> OutgoingMessage:
    return OutgoingMessage(
        text=str(data.get("text", "")),
        destination_id=str(data.get("destination_id", "")),
        channel_index=int(data.get("channel_index", 0)),
        want_ack=bool(data.get("want_ack", False)),
        reply_id=data.get("reply_id"),
    )


# ---------------------------------------------------------------------------
# SendResult
# ---------------------------------------------------------------------------
def send_result_to_dict(result: SendResult) -> dict:
    return {
        "accepted": result.accepted,
        "packet_id": result.packet_id,
        "error": error_to_dict(result.error) if result.error else None,
    }


def send_result_from_dict(data: dict) -> SendResult:
    return SendResult(
        accepted=bool(data.get("accepted", False)),
        packet_id=data.get("packet_id"),
        error=error_from_dict(data.get("error")),
    )


# ---------------------------------------------------------------------------
# CheckedSendResult (send_text_checked's response - a SendResult plus the
# radio-reported channel name the message went out on)
# ---------------------------------------------------------------------------
def checked_send_result_to_dict(result: CheckedSendResult) -> dict:
    return {
        "accepted": result.result.accepted,
        "packet_id": result.result.packet_id,
        "error": error_to_dict(result.result.error) if result.result.error else None,
        "channel_name": result.channel_name,
    }


def checked_send_result_from_dict(data: dict) -> CheckedSendResult:
    return CheckedSendResult(
        result=SendResult(
            accepted=bool(data.get("accepted", False)),
            packet_id=data.get("packet_id"),
            error=error_from_dict(data.get("error")),
        ),
        channel_name=str(data.get("channel_name", "")),
    )


# ---------------------------------------------------------------------------
# OutgoingWaypoint / WaypointResult
# ---------------------------------------------------------------------------
def outgoing_waypoint_to_dict(waypoint: OutgoingWaypoint) -> dict:
    return {
        "name": waypoint.name,
        "description": waypoint.description,
        "latitude": waypoint.latitude,
        "longitude": waypoint.longitude,
        "expire_at": waypoint.expire_at,
        "icon": waypoint.icon,
        "waypoint_id": waypoint.waypoint_id,
        "channel_index": waypoint.channel_index,
        "post_notification": waypoint.post_notification,
        "notification_text": waypoint.notification_text,
    }


def outgoing_waypoint_from_dict(data: dict) -> OutgoingWaypoint:
    return OutgoingWaypoint(
        name=str(data.get("name", "")),
        description=str(data.get("description", "")),
        latitude=float(data.get("latitude", 0.0)),
        longitude=float(data.get("longitude", 0.0)),
        expire_at=int(data.get("expire_at", 0)),
        icon=int(data.get("icon", 128205)),
        waypoint_id=data.get("waypoint_id"),
        channel_index=int(data.get("channel_index", 0)),
        post_notification=bool(data.get("post_notification", True)),
        notification_text=str(data.get("notification_text", "")),
    )


def waypoint_result_to_dict(result: WaypointResult) -> dict:
    return {
        "waypoint_id": result.waypoint_id,
        "waypoint_packet_id": result.waypoint_packet_id,
        "notification_packet_id": result.notification_packet_id,
    }


def waypoint_result_from_dict(data: dict) -> WaypointResult:
    return WaypointResult(
        waypoint_id=int(data.get("waypoint_id", 0)),
        waypoint_packet_id=data.get("waypoint_packet_id"),
        notification_packet_id=data.get("notification_packet_id"),
    )


# ---------------------------------------------------------------------------
# NodeUser / NodeInfo
# ---------------------------------------------------------------------------
def node_user_to_dict(user: Optional[NodeUser]) -> Optional[dict]:
    if user is None:
        return None
    return {
        "id": user.id,
        "long_name": user.long_name,
        "short_name": user.short_name,
        "hw_model": user.hw_model,
        "is_licensed": user.is_licensed,
    }


def node_user_from_dict(data: Optional[dict]) -> Optional[NodeUser]:
    if data is None:
        return None
    return NodeUser(
        id=str(data.get("id", "")),
        long_name=str(data.get("long_name", "")),
        short_name=str(data.get("short_name", "")),
        hw_model=str(data.get("hw_model", "")),
        is_licensed=bool(data.get("is_licensed", False)),
    )


def node_info_to_dict(node: NodeInfo) -> dict:
    return {
        "node_id": node.node_id,
        "num": node.num,
        "user": node_user_to_dict(node.user),
        "last_heard": node.last_heard,
        "snr": node.snr,
        "rssi": node.rssi,
        "hop_count": node.hop_count,
        "is_favorite": node.is_favorite,
        "device_metrics": node.device_metrics,
        "environment_metrics": node.environment_metrics,
        "power_metrics": node.power_metrics,
        "position": node.position,
    }


def node_info_from_dict(data: dict) -> NodeInfo:
    return NodeInfo(
        node_id=str(data.get("node_id", "")),
        num=int(data.get("num", 0)),
        user=node_user_from_dict(data.get("user")),
        last_heard=data.get("last_heard"),
        snr=data.get("snr"),
        rssi=data.get("rssi"),
        hop_count=data.get("hop_count"),
        is_favorite=bool(data.get("is_favorite", False)),
        device_metrics=dict(data.get("device_metrics") or {}),
        environment_metrics=dict(data.get("environment_metrics") or {}),
        power_metrics=dict(data.get("power_metrics") or {}),
        position=data.get("position"),
    )


# ---------------------------------------------------------------------------
# ChannelInfo
# ---------------------------------------------------------------------------
def channel_info_to_dict(channel: ChannelInfo) -> dict:
    return {"index": channel.index, "name": channel.name, "role": channel.role}


def channel_info_from_dict(data: dict) -> ChannelInfo:
    return ChannelInfo(
        index=int(data.get("index", 0)),
        name=str(data.get("name", "")),
        role=str(data.get("role", "")),
    )


# ---------------------------------------------------------------------------
# Received events (inbound traffic)
# ---------------------------------------------------------------------------
# One event on the wire is a discriminated envelope
#     {"kind": "text",     "text":     {<ReceivedTextEvent fields>}}
#     {"kind": "waypoint", "waypoint": {<ReceivedWaypointEvent fields>}}
# and drain_received's result is {"events": [<envelope>...], "dropped": n,
# "malformed": n, "connection_generation": n|null}.
#
# Field-by-field on BOTH sides, on purpose: the library's packet carries a
# protobuf `raw`, `decoded.payload` bytes and (waypoints) a second `raw`
# string, and none of it may be one careless `asdict()` away from the wire.
# to_dict emits exactly the whitelist below; from_dict reads exactly the
# whitelist and ignores every other key (a `raw` in the input is dropped, not
# adopted), and the event constructors reject non-plain types.
RECEIVED_KIND_TEXT = "text"
RECEIVED_KIND_WAYPOINT = "waypoint"
RECEIVED_KIND_NODEINFO = "nodeinfo"
RECEIVED_KIND_POSITION = "position"
RECEIVED_KIND_TELEMETRY = "telemetry"


def received_text_to_dict(event: ReceivedTextEvent) -> dict:
    return {
        "from_node_id": event.from_node_id,
        "to_node_id": event.to_node_id,
        "text": event.text,
        "received_at": event.received_at,
        "local_radio_node_id": event.local_radio_node_id,
        "packet_id": event.packet_id,
        "from_num": event.from_num,
        "to_num": event.to_num,
        "channel_index": event.channel_index,
        "reply_id": event.reply_id,
        "rx_time": event.rx_time,
        "rx_rssi": event.rx_rssi,
        "rx_snr": event.rx_snr,
        "hop_limit": event.hop_limit,
        "hop_start": event.hop_start,
        "relay_node": event.relay_node,
    }


def received_text_from_dict(data: dict) -> ReceivedTextEvent:
    return ReceivedTextEvent(
        from_node_id=data["from_node_id"],
        to_node_id=data["to_node_id"],
        text=data["text"],
        received_at=data["received_at"],
        local_radio_node_id=data["local_radio_node_id"],
        packet_id=data.get("packet_id"),
        from_num=data.get("from_num"),
        to_num=data.get("to_num"),
        channel_index=data.get("channel_index"),
        reply_id=data.get("reply_id"),
        rx_time=data.get("rx_time"),
        rx_rssi=data.get("rx_rssi"),
        rx_snr=data.get("rx_snr"),
        hop_limit=data.get("hop_limit"),
        hop_start=data.get("hop_start"),
        relay_node=data.get("relay_node"),
    )


def received_waypoint_to_dict(event: ReceivedWaypointEvent) -> dict:
    return {
        "waypoint_id": event.waypoint_id,
        "sender_id": event.sender_id,
        "name": event.name,
        "description": event.description,
        "received_at": event.received_at,
        "local_radio_node_id": event.local_radio_node_id,
        "packet_id": event.packet_id,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "icon": event.icon,
        "expire_at": event.expire_at,
        "channel_index": event.channel_index,
    }


def received_waypoint_from_dict(data: dict) -> ReceivedWaypointEvent:
    return ReceivedWaypointEvent(
        waypoint_id=data["waypoint_id"],
        sender_id=data["sender_id"],
        name=data["name"],
        description=data["description"],
        received_at=data["received_at"],
        local_radio_node_id=data["local_radio_node_id"],
        packet_id=data.get("packet_id"),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        icon=data.get("icon"),
        expire_at=data.get("expire_at"),
        channel_index=data.get("channel_index"),
    )


def received_nodeinfo_to_dict(event: ReceivedNodeInfoEvent) -> dict:
    return {
        "node_id": event.node_id,
        "sender_id": event.sender_id,
        "received_at": event.received_at,
        "local_radio_node_id": event.local_radio_node_id,
        "packet_id": event.packet_id,
        "long_name": event.long_name,
        "short_name": event.short_name,
        "hw_model": event.hw_model,
        "role": event.role,
        "is_licensed": event.is_licensed,
        "channel_index": event.channel_index,
        "rx_time": event.rx_time,
        "rx_rssi": event.rx_rssi,
        "rx_snr": event.rx_snr,
        "hop_limit": event.hop_limit,
        "hop_start": event.hop_start,
        "relay_node": event.relay_node,
    }


def received_nodeinfo_from_dict(data: dict) -> ReceivedNodeInfoEvent:
    return ReceivedNodeInfoEvent(
        node_id=data["node_id"],
        sender_id=data["sender_id"],
        received_at=data["received_at"],
        local_radio_node_id=data["local_radio_node_id"],
        packet_id=data.get("packet_id"),
        long_name=data.get("long_name"),
        short_name=data.get("short_name"),
        hw_model=data.get("hw_model"),
        role=data.get("role"),
        is_licensed=data.get("is_licensed"),
        channel_index=data.get("channel_index"),
        rx_time=data.get("rx_time"),
        rx_rssi=data.get("rx_rssi"),
        rx_snr=data.get("rx_snr"),
        hop_limit=data.get("hop_limit"),
        hop_start=data.get("hop_start"),
        relay_node=data.get("relay_node"),
    )


def received_position_to_dict(event: ReceivedPositionEvent) -> dict:
    return {
        "sender_id": event.sender_id,
        "received_at": event.received_at,
        "local_radio_node_id": event.local_radio_node_id,
        "packet_id": event.packet_id,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "altitude": event.altitude,
        "ground_speed": event.ground_speed,
        "sats_in_view": event.sats_in_view,
        "position_time": event.position_time,
        "channel_index": event.channel_index,
        "rx_time": event.rx_time,
        "rx_rssi": event.rx_rssi,
        "rx_snr": event.rx_snr,
        "hop_limit": event.hop_limit,
        "hop_start": event.hop_start,
        "relay_node": event.relay_node,
    }


def received_position_from_dict(data: dict) -> ReceivedPositionEvent:
    return ReceivedPositionEvent(
        sender_id=data["sender_id"],
        received_at=data["received_at"],
        local_radio_node_id=data["local_radio_node_id"],
        packet_id=data.get("packet_id"),
        latitude=data.get("latitude"),
        longitude=data.get("longitude"),
        altitude=data.get("altitude"),
        ground_speed=data.get("ground_speed"),
        sats_in_view=data.get("sats_in_view"),
        position_time=data.get("position_time"),
        channel_index=data.get("channel_index"),
        rx_time=data.get("rx_time"),
        rx_rssi=data.get("rx_rssi"),
        rx_snr=data.get("rx_snr"),
        hop_limit=data.get("hop_limit"),
        hop_start=data.get("hop_start"),
        relay_node=data.get("relay_node"),
    )


def received_telemetry_to_dict(event: ReceivedTelemetryEvent) -> dict:
    return {
        "sender_id": event.sender_id,
        "kind": event.kind,
        "metrics": dict(event.metrics),
        "received_at": event.received_at,
        "local_radio_node_id": event.local_radio_node_id,
        "packet_id": event.packet_id,
        "telemetry_time": event.telemetry_time,
        "channel_index": event.channel_index,
        "rx_time": event.rx_time,
        "rx_rssi": event.rx_rssi,
        "rx_snr": event.rx_snr,
        "hop_limit": event.hop_limit,
        "hop_start": event.hop_start,
        "relay_node": event.relay_node,
    }


def received_telemetry_from_dict(data: dict) -> ReceivedTelemetryEvent:
    return ReceivedTelemetryEvent(
        sender_id=data["sender_id"],
        kind=data["kind"],
        metrics=data["metrics"],
        received_at=data["received_at"],
        local_radio_node_id=data["local_radio_node_id"],
        packet_id=data.get("packet_id"),
        telemetry_time=data.get("telemetry_time"),
        channel_index=data.get("channel_index"),
        rx_time=data.get("rx_time"),
        rx_rssi=data.get("rx_rssi"),
        rx_snr=data.get("rx_snr"),
        hop_limit=data.get("hop_limit"),
        hop_start=data.get("hop_start"),
        relay_node=data.get("relay_node"),
    )


_RECEIVED_TO_DICT = {
    ReceivedTextEvent: (RECEIVED_KIND_TEXT, received_text_to_dict),
    ReceivedWaypointEvent: (RECEIVED_KIND_WAYPOINT, received_waypoint_to_dict),
    ReceivedNodeInfoEvent: (RECEIVED_KIND_NODEINFO, received_nodeinfo_to_dict),
    ReceivedPositionEvent: (RECEIVED_KIND_POSITION, received_position_to_dict),
    ReceivedTelemetryEvent: (RECEIVED_KIND_TELEMETRY, received_telemetry_to_dict),
}
_RECEIVED_FROM_DICT = {
    RECEIVED_KIND_TEXT: received_text_from_dict,
    RECEIVED_KIND_WAYPOINT: received_waypoint_from_dict,
    RECEIVED_KIND_NODEINFO: received_nodeinfo_from_dict,
    RECEIVED_KIND_POSITION: received_position_from_dict,
    RECEIVED_KIND_TELEMETRY: received_telemetry_from_dict,
}


def received_event_to_dict(event: ReceivedEvent) -> dict:
    for event_type, (kind, to_dict) in _RECEIVED_TO_DICT.items():
        if isinstance(event, event_type):
            return {"kind": kind, kind: to_dict(event)}
    raise TypeError(f"not a received event: {type(event).__name__}")


def received_event_from_dict(data: dict) -> ReceivedEvent:
    """Raises ValueError for an unknown/missing `kind` or a body that is not a
    dict, KeyError for a missing required field, TypeError/ValueError for a
    wrongly-typed or empty-where-forbidden one - an adapter that sends
    something malformed must be loud, not silently coerced."""
    kind = data.get("kind") if isinstance(data, dict) else None
    from_dict = _RECEIVED_FROM_DICT.get(kind)
    if from_dict is None:
        raise ValueError(f"unknown received event kind: {kind!r}")
    body = data.get(kind)
    if not isinstance(body, dict):
        raise ValueError(f"received {kind} event has no {kind!r} object")
    return from_dict(body)


def received_batch_to_dict(batch: ReceivedBatch) -> dict:
    return {
        "events": [received_event_to_dict(event) for event in batch.events],
        "dropped": batch.dropped,
        "malformed": batch.malformed,
        "connection_generation": batch.connection_generation,
    }


def received_batch_from_dict(data: dict) -> ReceivedBatch:
    """One malformed event is discarded and counted (added to whatever the
    adapter itself reported as malformed); it never costs the rest of the
    batch. A batch that is not even a dict/list-of-events shape raises."""
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        raise ValueError("received batch must be an object with an 'events' list")
    events = []
    malformed = 0
    for item in data["events"]:
        try:
            events.append(received_event_from_dict(item))
        except (KeyError, TypeError, ValueError):
            malformed += 1
    return ReceivedBatch(
        events=tuple(events),
        dropped=data.get("dropped") or 0,
        malformed=(data.get("malformed") or 0) + malformed,
        connection_generation=data.get("connection_generation"),
    )


# ---------------------------------------------------------------------------
# Envelope helpers - request/response framing per docs/BACKEND_API.md
# ---------------------------------------------------------------------------
def make_request(operation: str, params: dict, *, timeout: float) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "operation": operation,
        "params": params,
        "timeout": timeout,
    }


def make_ok_response(result: Any) -> dict:
    return {"protocol_version": PROTOCOL_VERSION, "ok": True, "result": result}


def make_error_response(error: TransportError) -> dict:
    return {"protocol_version": PROTOCOL_VERSION, "ok": False, "error": error_to_dict(error)}
