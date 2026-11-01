"""Passive MeshCenter marking (FX1 Phase C): which nodes in the list run MeshCenter too.

A node is marked when this node RECEIVES a MeshCenter control frame from it (any `MCA1:` direct message that decodes as a
real frame: an OFFER, a KEY_REQUEST, a KEY_ANNOUNCE, an acknowledgement ...). Nothing is ever sent for this: no broadcast,
no probe, no beacon. The mark is two timestamps on the node record, `mc_first_seen` and `mc_last_seen` (epoch seconds).

The mark is advisory. A frame is checked for its shape only (the `MCA1:` prefix, valid Base64URL, a canonical CBOR map with
a known protocol version and message type); nothing is verified here, so anyone who knows the format could make a node
look like MeshCenter. Trust still comes only from the key exchange; the mark just answers "can I expect this node to
understand a file transfer", and is what the "MeshCenter only" filter and the MC badge show.
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
from typing import Dict, Optional, Tuple

from meshsrv.attachments import codec

# the last-seen time is kept in memory on every frame but only written to disk this often (a first sighting always is)
PERSIST_EVERY_SECONDS = 300


def is_mca_frame(text: object) -> bool:
    """True when `text` is a well-formed MeshCenter control frame (shape only, not verified)."""
    if not isinstance(text, str) or not text.startswith(codec.TEXT_PREFIX):
        return False
    try:
        codec.peek_message_type(codec.from_text(text))
    except codec.CodecError:
        return False
    return True


def note_frame(node: dict, now: float) -> bool:
    """Mark `node` as seen sending a MeshCenter frame at `now`. Returns True when the change should be written to disk now
    (the first sighting, or the last-seen time moved by at least PERSIST_EVERY_SECONDS)."""
    first = node.get("mc_first_seen")
    previous = node.get("mc_last_seen")
    if not isinstance(first, (int, float)) or first <= 0:
        node["mc_first_seen"] = float(now)
        node["mc_last_seen"] = float(now)
        return True
    node["mc_last_seen"] = float(now)
    return not isinstance(previous, (int, float)) or now - previous >= PERSIST_EVERY_SECONDS


def mc_fields(node: dict) -> dict:
    """The two fields for an API projection (`None` for a node never seen sending a frame)."""
    first = node.get("mc_first_seen")
    last = node.get("mc_last_seen")
    ok = isinstance(first, (int, float)) and isinstance(last, (int, float)) and first > 0 and last > 0
    return {"mc_first_seen": float(first) if ok else None, "mc_last_seen": float(last) if ok else None}


def is_meshcenter_node(node: Optional[dict]) -> bool:
    return bool(node) and mc_fields(node)["mc_last_seen"] is not None


# ---- back-fill from what MCAttach already knows --------------------------------------------------------------------------
#
# A node with a key binding (any trust state: the binding exists because a KEY_ANNOUNCE was received from it) is MeshCenter
# by definition, and so is a node that sent an OFFER or acknowledged one. Before this marking existed none of that reached
# the node list, so at startup the marks are filled from the MCAttach database (read only). It only ever moves a mark
# forward in knowledge: `mc_first_seen` becomes the earliest time known, `mc_last_seen` the latest, so running it again, or
# after live frames were noted, changes nothing that is already right.

_TIMES_SQL = (
    # (address, first, last) per source of "a MeshCenter frame came from this address"
    "SELECT transport_address, bound_at, MAX(bound_at, COALESCE(pending_detected_at, 0)) FROM mca_recipient_bindings",
    "SELECT reply_route_id, MIN(created_at), MAX(created_at) FROM attachments "
    "WHERE direction = 'received' AND reply_route_id IS NOT NULL GROUP BY reply_route_id",
    "SELECT route_id, MIN(ack_at), MAX(ack_at) FROM attachment_deliveries WHERE ack_at IS NOT NULL GROUP BY route_id",
)


def known_frame_times(conn: sqlite3.Connection) -> Dict[str, Tuple[float, float]]:
    """address -> (earliest, latest) time a MeshCenter frame is known to have come from it, from the MCAttach tables."""
    found: Dict[str, Tuple[float, float]] = {}
    for sql in _TIMES_SQL:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error:
            continue                                   # a table an older schema does not have: that source adds nothing
        for address, first, last in rows:
            if not isinstance(address, str) or not isinstance(first, (int, float)) or not isinstance(last, (int, float)):
                continue
            if first <= 0 or last <= 0:
                continue
            previous = found.get(address)
            found[address] = (min(first, previous[0]), max(last, previous[1])) if previous else (float(first), float(last))
    return found


def backfill_from_times(nodes: dict, times: Dict[str, Tuple[float, float]], now: float) -> int:
    """Mark every node of `nodes` that appears in `times`; returns how many node records changed. A node the list does not
    know is skipped (there is nothing to show it on); a time that is unknown becomes `now`."""
    changed = 0
    for address, (first, last) in times.items():
        node = nodes.get(address)
        if node is None:
            continue
        old_first, old_last = node.get("mc_first_seen"), node.get("mc_last_seen")
        have = isinstance(old_first, (int, float)) and old_first > 0 and isinstance(old_last, (int, float)) and old_last > 0
        new_first = min(first, old_first) if have else (first or now)
        new_last = max(last, old_last) if have else (last or now)
        if have and (new_first, new_last) == (old_first, old_last):
            continue
        node["mc_first_seen"], node["mc_last_seen"] = float(new_first), float(new_last)
        changed += 1
    return changed


def backfill_from_database(nodes: dict, db_path: str, now: float) -> int:
    """Open the MCAttach database READ ONLY (a second connection never writes and never touches the worker's), fill the marks.
    Returns the number of node records changed; 0 when there is no database yet. Never raises."""
    if not db_path or not os.path.isfile(db_path):
        return 0
    try:
        uri = pathlib.Path(db_path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        return 0
    try:
        return backfill_from_times(nodes, known_frame_times(conn), now)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
