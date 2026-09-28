"""Shared Core ingest for received text messages and waypoints.

One place decides what an inbound text/waypoint does to MeshCenter's state -
dedup, node update, chat routing, replies, MCAttach dispatch, the waypoint
store - whichever transport received it:

    Serial   `--listen` CLI line -> parse -> ReceivedTextEvent / ReceivedWaypointEvent --+
                                                                                          +--> ingest_received_*()
    TCP      adapter drain_received() -----------> ReceivedTextEvent / ReceivedWaypointEvent --+

This is the logic that used to live inline at the tail of server.py's
`_handle_listener_line()` (and in `update_node()` / `process_waypoint_line()`),
moved here unchanged in behaviour so a second transport cannot grow a second,
slowly diverging copy. The order of side effects is deliberate and preserved:

  text:  radio_event("text") -> packet-id dedup (BEFORE the node update: a
         duplicate must not touch the node) -> node update -> text-fallback
         dedup (AFTER it: a repeated text still refreshes the node) -> ignored
         node -> chat routing -> reply reference -> add_message -> MCAttach.

State (nodes, chats, messages, seen ids, the waypoint store, the lock) belongs
to server.py; it is handed in through `InboundDeps`, built per call because some
of it (seen_ids) is rebound by background cleanup.

SAFETY GATE: every event names the radio it came from (`local_radio_node_id`).
Before anything is persisted it must equal the active accepted profile's node
id; otherwise the event is dropped with a warning. Events buffered from radio A
can therefore never be written into radio B's profile after a switch.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from meshsrv.radio_transport import (
    ReceivedNodeInfoEvent,
    ReceivedPositionEvent,
    ReceivedTelemetryEvent,
    ReceivedTextEvent,
    ReceivedWaypointEvent,
)

# Outcomes (strings, so callers and tests can assert without importing enums).
STORED = "stored"
CREATED = "created"
UPDATED = "updated"
DUPLICATE_PACKET = "duplicate_packet"
DUPLICATE_TEXT = "duplicate_text"
DUPLICATE = "duplicate"
IGNORED_NODE = "ignored_node"
STALE_RADIO = "stale_radio"
NO_POSITION = "no_position"
SKIPPED_LOCAL = "skipped_local"
NO_NODE_ID = "no_node_id"
NO_VALUES = "no_values"

BROADCAST_RECIPIENT = "^all"
MCA_PREFIX = "MCA1:"


@dataclass(frozen=True)
class SerialLineContext:
    """Serial-only, Core-local extras that are NOT part of the neutral event:
    the raw CLI line (kept for the node log exactly as before), the sender name
    the CLI printed when it gave no node id, the role field, and the SNR exactly
    as the line spelled it. Never crosses a process boundary; a TCP event has
    none of this and needs none."""

    raw_line: str = ""
    sender_hint: str = ""
    role: Optional[str] = None
    rx_snr_text: Optional[str] = None


@dataclass
class InboundDeps:
    """Everything the ingest needs from server.py, injected (the project's
    dependency-injection-by-parameter convention). Built fresh per call."""

    state_lock: Any
    active_radio_node_id: Callable[[], str]
    local_node_id: str
    channel_chat_id: Callable[[Any], str]
    CHANNEL_CHAT_ID: str
    CHANNEL_CHAT_NAME: str
    radio_event: Callable[..., Any]
    seen_ids: set
    nodes: dict
    chats: dict
    get_node_name: Callable[[str], str]
    get_node_info: Callable[[str], dict]
    infer_node_id_from_sender: Callable[[str], str]
    is_duplicate_text: Callable[..., bool]
    log_node_event: Callable[..., Any]
    ensure_chat: Callable[..., Any]
    save_chats: Callable[[], Any]
    save_nodes: Callable[[], Any]
    find_message_by_packet_id: Callable[..., Any]
    build_reply_reference: Callable[[Any], Any]
    add_message: Callable[..., Any]
    dispatch_mca: Callable[..., Any]
    now: Callable[[], str]
    time: Callable[[], float]
    waypoint_store: Any = None
    get_waypoint_sender_name: Optional[Callable[[str], str]] = None
    log_system_event: Optional[Callable[..., Any]] = None
    log: Callable[..., Any] = print
    # NodeInfo/Position/Telemetry ingest (PR C): the same merge/apply helpers
    # server.py's own serial parsers (process_nodeinfo(),
    # process_received_nodeinfo_line()) were extracted into, so TCP and serial
    # build node state through one shared rule set - see each helper's own
    # docstring in server.py for the exact behavior preserved.
    merge_nodeinfo_into_node: Optional[Callable[..., dict]] = None
    merge_position_into_node: Optional[Callable[..., dict]] = None
    telemetry_values_from_metrics: Optional[Callable[..., dict]] = None
    apply_node_telemetry: Optional[Callable[..., bool]] = None


# --------------------------------------------------------------------------
# Observability (counters only - never message content)
# --------------------------------------------------------------------------
_stats_lock = threading.Lock()
_stats = {
    "text_stored": 0, "text_duplicate_packet": 0, "text_duplicate_text": 0, "text_ignored_node": 0,
    "waypoint_created": 0, "waypoint_updated": 0, "waypoint_duplicate": 0, "waypoint_no_position": 0,
    "nodeinfo_stored": 0, "nodeinfo_skipped_local": 0, "nodeinfo_no_node_id": 0,
    "position_stored": 0, "position_no_position": 0, "position_no_node_id": 0,
    "telemetry_stored": 0, "telemetry_no_values": 0, "telemetry_no_node_id": 0,
    "stale_identity_dropped": 0,
}


def _count(name: str) -> None:
    with _stats_lock:
        _stats[name] += 1


def get_inbound_stats() -> dict:
    with _stats_lock:
        return dict(_stats)


def reset_inbound_stats() -> None:
    with _stats_lock:
        for key in _stats:
            _stats[key] = 0


# --------------------------------------------------------------------------
# Safety gate
# --------------------------------------------------------------------------
def _same_radio(event_radio: str, active_radio: str) -> bool:
    return str(event_radio or "").strip().lower() == str(active_radio or "").strip().lower()


def _accepts(event, deps: InboundDeps, kind: str) -> bool:
    active = deps.active_radio_node_id()
    if _same_radio(event.local_radio_node_id, active):
        return True
    _count("stale_identity_dropped")
    # No content in the log line: message text is private.
    deps.log(
        f"[INBOUND] Dropped a received {kind} from radio {event.local_radio_node_id}: "
        f"the active accepted radio is {active}",
        flush=True,
    )
    return False


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------
def _signal_text(value) -> Optional[str]:
    return None if value is None else str(value)


def _loggable_text(value, context: Optional[SerialLineContext]):
    """The node log has always carried message text for serial (unchanged). A
    text that arrived without a CLI line is private content nobody asked to have
    in a diagnostic file: log only its length."""
    if context is not None or not value:
        return value
    return f"<{len(str(value))} chars>"


def _update_node_from_received_text(
    event: ReceivedTextEvent,
    node_id: str,
    sender: str,
    deps: InboundDeps,
    context: Optional[SerialLineContext],
) -> str:
    """The old update_node(line, sender, text), fed by the event instead of by
    re-parsing a CLI line. Signal values are stored as STRINGS, as before."""
    if not node_id:
        return ""

    raw_line = context.raw_line if context else ""

    if node_id == deps.local_node_id:
        deps.log_node_event(
            "SKIP_LOCAL_NODE",
            "TEXT_MESSAGE",
            node_id,
            extra={"sender": sender, "text": event.text if context else f"<{len(event.text)} chars>"},
            raw=raw_line or None,
        )
        return node_id

    rssi = _signal_text(event.rx_rssi)
    snr = context.rx_snr_text if (context and context.rx_snr_text is not None) else _signal_text(event.rx_snr)
    hop_start = _signal_text(event.hop_start)
    relay_node = _signal_text(event.relay_node)
    role = context.role if context else None
    text = event.text

    name = deps.get_node_name(node_id)
    info = deps.get_node_info(node_id)

    with deps.state_lock:
        old = deps.nodes.get(node_id, {})

        old_snapshot = {
            "name": old.get("name"),
            "short_name": old.get("short_name"),
            "hw_model": old.get("hw_model"),
            "role": old.get("role"),
            "rssi": old.get("rssi"),
            "snr": old.get("snr"),
            "hop_start": old.get("hop_start"),
            "relay_node": old.get("relay_node"),
            "last_text": _loggable_text(old.get("last_text"), context),
        }

        # A text message never renames a node: only NODEINFO does.
        stable_name = old.get("name") or name

        node = dict(old)
        node.update({
            "name": stable_name,
            "node_id": node_id,
            "last_seen": deps.time(),
            "last_time": deps.now(),
            "rssi": rssi or old.get("rssi"),
            "snr": snr or old.get("snr"),
            "hop_start": hop_start or old.get("hop_start", ""),
            "relay_node": relay_node or old.get("relay_node", ""),
            "last_text": text or old.get("last_text", ""),
            "short_name": info.get("short_name") or old.get("short_name", "") or node_id[-4:],
            "hw_model": info.get("hw_model") or old.get("hw_model", ""),
            "role": role or old.get("role", "CLIENT"),
            "ignored": old.get("ignored", False),
            "favorite": old.get("favorite", False),
            # Text packets carry no coordinates and must not erase a known position.
            "position": old.get("position"),
        })
        deps.nodes[node_id] = node

        new_snapshot = {
            "name": node.get("name"),
            "short_name": node.get("short_name"),
            "hw_model": node.get("hw_model"),
            "role": node.get("role"),
            "rssi": node.get("rssi"),
            "snr": node.get("snr"),
            "hop_start": node.get("hop_start"),
            "relay_node": node.get("relay_node"),
            "last_text": _loggable_text(node.get("last_text"), context),
        }

        deps.log_node_event(
            "UPDATE_NODE",
            "TEXT_MESSAGE",
            node_id,
            old=old_snapshot,
            new=new_snapshot,
            extra={
                "sender": sender,
                "text": text if context else f"<{len(text)} chars>",
                "line_has_longName": "longName" in raw_line,
            },
            raw=raw_line or None,
        )

        if node_id.startswith("!"):
            deps.ensure_chat(node_id, node.get("name"), force=True)

        deps.save_nodes()

    return node_id


def ingest_received_text(
    event: ReceivedTextEvent,
    deps: InboundDeps,
    *,
    line_context: Optional[SerialLineContext] = None,
) -> str:
    """Store one received text message. Returns an outcome constant (STORED,
    DUPLICATE_PACKET, DUPLICATE_TEXT, IGNORED_NODE, STALE_RADIO)."""
    if not _accepts(event, deps, "text"):
        return STALE_RADIO

    text = event.text
    deps.radio_event("text")

    pid = event.packet_id
    if pid:
        with deps.state_lock:
            if pid in deps.seen_ids:
                _count("text_duplicate_packet")
                return DUPLICATE_PACKET
            deps.seen_ids.add(pid)

    # The sender: the id the source parsed, else (serial only) the name the CLI
    # printed, resolved back to a node if it is one we know.
    node_id = event.from_node_id or ""
    hint = line_context.sender_hint if line_context else ""
    sender = deps.get_node_name(node_id) if node_id else (hint or "RX")
    if not node_id:
        node_id = deps.infer_node_id_from_sender(sender)
    node_id = _update_node_from_received_text(event, node_id, sender, deps, line_context)

    if node_id:
        sender = deps.get_node_name(node_id)

    if deps.is_duplicate_text(sender, text, node_id):
        _count("text_duplicate_text")
        return DUPLICATE_TEXT

    if node_id and deps.nodes.get(node_id, {}).get("ignored", False):
        _count("text_ignored_node")
        return IGNORED_NODE

    is_channel = event.to_node_id == BROADCAST_RECIPIENT
    chat_id = deps.CHANNEL_CHAT_ID

    if is_channel:
        channel_index = max(0, min(7, event.channel_index or 0))
        chat_id = deps.channel_chat_id(channel_index)
        if chat_id not in deps.chats:
            with deps.state_lock:
                deps.chats[chat_id] = {
                    "id": chat_id,
                    "name": deps.CHANNEL_CHAT_NAME if channel_index == 0 else f"Channel {channel_index}",
                    "type": "channel",
                    "last_message": "",
                    "last_time": "",
                    "unread": 0,
                }
                deps.save_chats()
    elif node_id and node_id.startswith("!"):
        # A direct message goes to the sender's own chat (add_message folds a
        # message from our own node back into the primary channel).
        chat_id = node_id

    if chat_id.startswith("!") and chat_id != deps.local_node_id:
        with deps.state_lock:
            deps.ensure_chat(chat_id, deps.get_node_name(chat_id), force=False)

    reply_to = None
    if event.reply_id:
        with deps.state_lock:
            original = (
                deps.find_message_by_packet_id(event.reply_id, chat_id)
                or deps.find_message_by_packet_id(event.reply_id)
            )
            reply_to = deps.build_reply_reference(original)
        # TEMPORARY (Reply Metadata Consistency investigation, PR 1 - remove
        # once the live root cause is confirmed): counts and identifiers only,
        # never message text/quoted text - see this project's own diagnostic
        # logging convention elsewhere in this module.
        deps.log(
            f"[INBOUND REPLY] packet_id={pid} reply_id={event.reply_id} chat_id={chat_id} "
            f"original_found={'true' if original else 'false'} "
            f"matched_original_packet_id={(original or {}).get('packet_id')} "
            f"transport={'serial' if line_context is not None else 'tcp'} "
            f"local_radio_node_id={event.local_radio_node_id}",
            flush=True,
        )

    with deps.state_lock:
        deps.add_message(
            "rx",
            sender,
            text,
            node_id,
            chat_id,
            reply_to=reply_to,
            packet_id=pid,
        )
    _count("text_stored")

    # MCAttach (spec 19.1): a cheap prefix check only, after the normal message
    # is already saved - never block the caller on MCA parsing/crypto/Relay.
    # Direct messages only (Stage 1: supports_channel=False).
    if not is_channel and node_id and text.startswith(MCA_PREFIX):
        try:
            deps.dispatch_mca(text, node_id, pid, event.channel_index)
        except Exception as error:
            deps.log(f"[MCA] listener dispatch error: {error}", flush=True)

    return STORED


# --------------------------------------------------------------------------
# Waypoints
# --------------------------------------------------------------------------
def provenance_snapshot(event: ReceivedWaypointEvent, source: str) -> str:
    """What is stored in the waypoint row's `raw_packet` when there is no CLI
    line to keep: a small, safe description of where the waypoint came from -
    never the library's packet, which carries protobuf and payload bytes."""
    return json.dumps(
        {
            "source": source,
            "packet_id": event.packet_id,
            "sender_id": event.sender_id,
            "channel_index": event.channel_index,
        },
        sort_keys=True,
    )


def ingest_received_waypoint(
    event: ReceivedWaypointEvent,
    deps: InboundDeps,
    *,
    raw_packet: Optional[str] = None,
    source: str = "tcp",
) -> str:
    """Upsert one received waypoint into the waypoint store. `raw_packet` is the
    Core-local CLI line (serial only, kept for diagnostics as before); without
    one a provenance snapshot is stored instead. Returns CREATED, UPDATED,
    DUPLICATE, NO_POSITION or STALE_RADIO."""
    if not _accepts(event, deps, "waypoint"):
        return STALE_RADIO

    if event.latitude is None or event.longitude is None:
        # A waypoint without a position (e.g. a remote delete) is out of scope,
        # exactly as the serial parser has always skipped it.
        _count("waypoint_no_position")
        return NO_POSITION

    saved = deps.waypoint_store.upsert({
        "waypoint_id": event.waypoint_id,
        "sender_id": event.sender_id,
        "name": event.name,
        "description": event.description,
        "latitude": event.latitude,
        "longitude": event.longitude,
        "icon": event.icon,
        "expire_at": event.expire_at,
        "channel_index": event.channel_index,
        "received_at": event.received_at,
        "raw_packet": raw_packet if raw_packet is not None else provenance_snapshot(event, source),
    })
    outcome = saved.pop("_event", "created")
    if outcome == "duplicate":
        _count("waypoint_duplicate")
        return DUPLICATE

    sender_name = deps.get_node_name(saved.get("sender_id")) if saved.get("sender_id") else "Unknown"
    name = saved.get("name") or f"Waypoint {saved.get('waypoint_id')}"
    action = "Updated" if outcome == "updated" else "Received"
    deps.log(
        f"[WAYPOINT] {action}: {name}; sender={sender_name} "
        f"({saved.get('sender_id') or 'unknown'}); "
        f"lat={saved.get('latitude')}; lon={saved.get('longitude')}; "
        f"channel={saved.get('channel_index')}",
        flush=True,
    )
    deps.log_system_event(
        title=f"Waypoint {outcome}",
        level="INFO",
        details=f"{name}; sender: {sender_name}; "
        f"coordinates: {saved.get('latitude')}, {saved.get('longitude')}",
        source="waypoint",
    )
    _count("waypoint_updated" if outcome == "updated" else "waypoint_created")
    return UPDATED if outcome == "updated" else CREATED


# --------------------------------------------------------------------------
# NodeInfo / Position / Telemetry (PR C)
# --------------------------------------------------------------------------
def ingest_received_nodeinfo(event: ReceivedNodeInfoEvent, deps: InboundDeps) -> str:
    """Merge one received NodeInfo into node state via
    deps.merge_nodeinfo_into_node() - server.py's own process_nodeinfo() node-
    merge core, reused unchanged so TCP and serial build the exact same node
    dict from the same rules. Returns STORED, SKIPPED_LOCAL, NO_NODE_ID or
    STALE_RADIO."""
    if not _accepts(event, deps, "nodeinfo"):
        return STALE_RADIO

    node_id = event.node_id
    if not node_id:
        _count("nodeinfo_no_node_id")
        return NO_NODE_ID

    if node_id == deps.local_node_id:
        # process_nodeinfo() skips the local node entirely - preserved as-is.
        _count("nodeinfo_skipped_local")
        return SKIPPED_LOCAL

    with deps.state_lock:
        old = deps.nodes.get(node_id, {})
        node = deps.merge_nodeinfo_into_node(
            node_id,
            old,
            long_name=event.long_name or "",
            short_name=event.short_name or "",
            hw_model=event.hw_model or "",
            role=event.role or "",
            rssi=_signal_text(event.rx_rssi),
            snr=_signal_text(event.rx_snr),
            hop_start=_signal_text(event.hop_start),
            relay_node=_signal_text(event.relay_node),
        )
        deps.nodes[node_id] = node
        if node_id.startswith("!"):
            deps.ensure_chat(node_id, node.get("name"), force=True)
        deps.save_nodes()

    _count("nodeinfo_stored")
    return STORED


def ingest_received_position(event: ReceivedPositionEvent, deps: InboundDeps) -> str:
    """Merge one received Position into the reporting node's `position` field
    via deps.merge_position_into_node() - the same overlay-merge core
    process_received_nodeinfo_line() uses for its own bundled position. Unlike
    NodeInfo, a bare Position never had a serial counterpart (the CLI --listen
    path only ever saw position bundled inside a "Received nodeinfo:" line),
    so this only ever touches `position` plus last-seen bookkeeping - it does
    not rename a node, set short_name/hw_model/role, or touch rssi/snr/hop
    fields, none of which a Position packet carries any precedent for setting.
    `ground_speed`/`sats_in_view` are new position-dict keys with no serial
    equivalent; `latitude`/`longitude`/`altitude`/`time` match the existing
    nodeinfo-position schema exactly (see _normalize_nodeinfo_position()).
    Returns STORED, NO_POSITION, NO_NODE_ID or STALE_RADIO."""
    if not _accepts(event, deps, "position"):
        return STALE_RADIO

    node_id = event.sender_id
    if not node_id:
        _count("position_no_node_id")
        return NO_NODE_ID

    if event.latitude is None or event.longitude is None:
        _count("position_no_position")
        return NO_POSITION

    position = {"latitude": event.latitude, "longitude": event.longitude}
    if event.altitude is not None:
        position["altitude"] = event.altitude
    if event.position_time is not None:
        position["time"] = event.position_time
    if event.ground_speed is not None:
        position["ground_speed"] = event.ground_speed
    if event.sats_in_view is not None:
        position["sats_in_view"] = event.sats_in_view

    with deps.state_lock:
        old = deps.nodes.get(node_id, {})
        node = dict(old)
        if not old:
            info = deps.get_node_info(node_id)
            node.update({
                "node_id": node_id,
                "name": deps.get_node_name(node_id),
                "short_name": info.get("short_name") or node_id[-4:],
                "hw_model": info.get("hw_model") or "",
                "role": "CLIENT",
                "ignored": False,
                "favorite": False,
                "last_text": "",
            })
        node["last_seen"] = deps.time()
        node["last_time"] = deps.now()
        node = deps.merge_position_into_node(node, old, position)
        deps.nodes[node_id] = node
        if node_id.startswith("!"):
            deps.ensure_chat(node_id, node.get("name"), force=False)
        deps.save_nodes()

    _count("position_stored")
    return STORED


def ingest_received_telemetry(event: ReceivedTelemetryEvent, deps: InboundDeps) -> str:
    """Thin wrapper over the already transport-agnostic
    deps.apply_node_telemetry(..., source="tcp") - nothing about telemetry
    persistence is duplicated here. Only builds the `values` dict (via
    deps.telemetry_values_from_metrics(), the same mapping
    process_received_nodeinfo_line() uses) by routing event.metrics into
    whichever of device/environment/power matches event.kind - a Telemetry
    packet is a protobuf oneof, so the other two are always empty. Returns
    STORED, NO_VALUES, NO_NODE_ID or STALE_RADIO."""
    if not _accepts(event, deps, "telemetry"):
        return STALE_RADIO

    node_id = event.sender_id
    if not node_id:
        _count("telemetry_no_node_id")
        return NO_NODE_ID

    device_metrics = event.metrics if event.kind == "device" else {}
    environment_metrics = event.metrics if event.kind == "environment" else {}
    power_metrics = event.metrics if event.kind == "power" else {}

    values = deps.telemetry_values_from_metrics(device_metrics, environment_metrics, power_metrics)
    updated = deps.apply_node_telemetry(node_id, values, source="tcp")
    if not updated:
        _count("telemetry_no_values")
        return NO_VALUES

    _count("telemetry_stored")
    return STORED
