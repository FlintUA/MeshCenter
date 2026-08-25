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
    ConnectionDescriptor,
    ConnectionInfo,
    ConnectionState,
    ConnectionType,
    NodeInfo,
    NodeUser,
    OutgoingMessage,
    OutgoingWaypoint,
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
