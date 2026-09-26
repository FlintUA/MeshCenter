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

from meshsrv.radio_transport import ReceivedTextEvent, ReceivedWaypointEvent

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


# --------------------------------------------------------------------------
# Observability (counters only - never message content)
# --------------------------------------------------------------------------
_stats_lock = threading.Lock()
_stats = {
    "text_stored": 0, "text_duplicate_packet": 0, "text_duplicate_text": 0, "text_ignored_node": 0,
    "waypoint_created": 0, "waypoint_updated": 0, "waypoint_duplicate": 0, "waypoint_no_position": 0,
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
