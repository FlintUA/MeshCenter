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

from typing import Optional

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
