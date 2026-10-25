"""SIG4: which node was the LAST relay of a received packet.

Firmware 2.6+ puts `relay_node` in every MeshPacket: only the LOW BYTE of the node number of the node that
transmitted the packet to us last (for a packet heard directly that is the sender itself). One byte does not name
a node: on a busy mesh several known nodes share it (on a live node with 638 known nodes, 442 of 443 relay bytes
matched more than three nodes). So the byte is matched against a narrow candidate set:

  1. nodes heard DIRECTLY (a packet of their own with hop_start == hop_limit) in the last 24 hours - the last
     relay transmitted straight to us, so it is a node we can hear;
  2. only if there is none: nodes heard at all in the last 2 hours (the recent-window rule).

The result is one of
  direct     the packet was heard directly (hop_start == hop_limit) and the byte is the sender's own: the sender
  resolved   exactly one candidate
  ambiguous  several candidates (they are listed, most recently heard directly first)
  unknown    a byte, but no candidate

Pure functions, stdlib only: no Flask, no state; the caller passes the node table and the clock.
"""
from __future__ import annotations

from typing import Any, Optional

DIRECT_WINDOW_S = 24 * 3600
RECENT_WINDOW_S = 2 * 3600
MAX_LISTED = 4

DIRECT = "direct"
RESOLVED = "resolved"
AMBIGUOUS = "ambiguous"
UNKNOWN = "unknown"
STATUSES = (DIRECT, RESOLVED, AMBIGUOUS, UNKNOWN)


def low_byte(node_id: Any) -> Optional[int]:
    """'!aabbccdd' -> 0xdd, anything else -> None."""
    text = str(node_id or "")
    if len(text) != 9 or not text.startswith("!"):
        return None
    try:
        return int(text[1:], 16) & 0xFF
    except ValueError:
        return None


def is_direct(hop_start: Any, hop_limit: Any) -> bool:
    """A packet is heard directly when it was never retransmitted: hop_start == hop_limit. hop_start 0 means
    "unknown" (older firmware), not "no hops"."""
    try:
        start, limit = int(hop_start), int(hop_limit)
    except (TypeError, ValueError):
        return False
    return start > 0 and start == limit


def note_direct(node: dict, hop_start: Any, hop_limit: Any, now: float) -> None:
    """Remember on the node record that its own packet was heard directly just now (`last_direct`, epoch seconds)."""
    if isinstance(node, dict) and is_direct(hop_start, hop_limit):
        node["last_direct"] = float(now)


def clean_relay(value: Any) -> Optional[dict]:
    """What may be stored on a message: a small dict of known keys and plain values, or None."""
    if not isinstance(value, dict) or value.get("status") not in STATUSES:
        return None
    out: dict = {"status": value["status"]}
    node_id = value.get("node_id")
    if isinstance(node_id, str) and low_byte(node_id) is not None:
        out["node_id"] = node_id
    short = value.get("short_name")
    if isinstance(short, str) and short.strip():
        out["short_name"] = short.strip()[:16]
    basis = value.get("basis")
    if basis in ("direct", "direct24h", "recent2h", "none"):
        out["basis"] = basis
    count = value.get("count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        out["count"] = min(count, 999)
    candidates = []
    for entry in (value.get("candidates") or [])[:MAX_LISTED]:
        if isinstance(entry, dict) and isinstance(entry.get("node_id"), str) and low_byte(entry["node_id"]) is not None:
            candidates.append({"node_id": entry["node_id"], "short_name": str(entry.get("short_name") or "")[:16]})
    if candidates:
        out["candidates"] = candidates
    return out


def _short(node: dict, node_id: str) -> str:
    return str(node.get("short_name") or node.get("name") or node_id[-4:])[:16]


def _entry(node_id: str, node: dict) -> dict:
    return {"node_id": node_id, "short_name": _short(node, node_id)}


def resolve_relay(relay_byte: Any, *, sender_id: str, hop_start: Any, hop_limit: Any, nodes: dict,
                  local_node_id: str = "", now: float) -> Optional[dict]:
    """The relay of one received packet, or None when the packet carries no relay byte (older firmware, MQTT).
    `nodes` is the node table ({"!id": {...}}); it is only read."""
    try:
        byte = int(relay_byte)
    except (TypeError, ValueError):
        return None
    if not 1 <= byte <= 255:
        return None

    if is_direct(hop_start, hop_limit) and low_byte(sender_id) == byte:
        node = nodes.get(sender_id) or {}
        return {"status": DIRECT, "node_id": sender_id, "short_name": _short(node, sender_id), "basis": "direct"}

    def matching(window: float, field: str):
        out = []
        for node_id, node in nodes.items():
            if node_id in (sender_id, local_node_id) or low_byte(node_id) != byte or not isinstance(node, dict):
                continue
            stamp = node.get(field)
            if isinstance(stamp, (int, float)) and 0 <= now - stamp <= window:
                out.append((stamp, node_id, node))
        out.sort(key=lambda item: item[0], reverse=True)
        return out

    for window, field, basis in ((DIRECT_WINDOW_S, "last_direct", "direct24h"), (RECENT_WINDOW_S, "last_seen", "recent2h")):
        found = matching(window, field)
        if not found:
            continue
        if len(found) == 1:
            _stamp, node_id, node = found[0]
            return {"status": RESOLVED, "node_id": node_id, "short_name": _short(node, node_id), "basis": basis}
        return {"status": AMBIGUOUS, "basis": basis, "count": len(found),
                "candidates": [_entry(node_id, node) for _stamp, node_id, node in found[:MAX_LISTED]]}
    return {"status": UNKNOWN, "basis": "none"}
