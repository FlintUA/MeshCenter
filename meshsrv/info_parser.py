"""Shared, pure text-parsing helpers for `meshtastic --info` CLI output
(F3, generalizing meshsrv/radio_identity.py's own string-aware JSON
extraction to every `--info` consumer in server.py).

No imports from server.py or any other DI-injected module - this stays a
pure function library the way meshsrv/radio_identity.py's own parser
helpers already are, so it is safe to import from both server.py and
meshsrv/radio_identity.py without a circular dependency.

Root cause this replaces: server.py's extract_json_block() (and, before
this module existed, meshsrv/radio_identity.py's own near-identical
private copy) counted `{`/`}` characters without knowing about JSON
strings - a `{` or `}` inside a quoted value (a node's longName, which
comes from the mesh and is attacker/environment-controlled from Core's
point of view) desynchronizes the brace count and truncates or corrupts
the "block" it returns. find_json_object() below sidesteps this entirely
by locating the opening brace and then handing the rest of the parse to
the real JSON parser's own incremental decoder (json.JSONDecoder.raw_decode),
which is correctly string-aware by construction - a brace inside a string
literal can never desynchronize it, because the decoder itself knows it
is inside a string.
"""

from __future__ import annotations

import json
import re
from typing import Any


def find_json_object(text: str, marker: str) -> dict[str, Any] | None:
    """Finds `marker` in `text`, then the first '{' after it, and parses
    the JSON object starting there using json.JSONDecoder().raw_decode() -
    the real JSON parser's own incremental decode, which naturally stops
    at the object's real closing brace no matter what a string value
    inside it contains. Never raises: a missing marker, a missing '{', or
    malformed/truncated JSON all return None. Returns None (not the
    parsed value) if the JSON at that position isn't an object."""
    if not text or not marker:
        return None
    marker_pos = text.find(marker)
    if marker_pos < 0:
        return None
    brace_pos = text.find("{", marker_pos)
    if brace_pos < 0:
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(text, brace_pos)
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def parse_info_nodes(output: str) -> dict[str, dict[str, Any]]:
    """The `{node_id: node_data}` object following "Nodes in mesh:" in
    `meshtastic --info` output. Never raises; a missing marker or
    malformed JSON returns {} (matching parse_nodes_from_info()'s and
    meshsrv/radio_identity.py's _parse_nodes()'s existing behavior for
    well-formed output)."""
    value = find_json_object(output, "Nodes in mesh:")
    return value if isinstance(value, dict) else {}


def _normalize_node_id(value: Any) -> str:
    # Mirrors meshsrv/radio_identity.py's _normalize_node_id() exactly
    # (not imported from there, to keep this module free of any
    # dependency on radio_identity - radio_identity depends on THIS
    # module, not the other way around).
    text = str(value or "").strip().lower()
    if re.fullmatch(r"![0-9a-f]{8}", text):
        return text
    try:
        number = int(text, 0)
    except (TypeError, ValueError):
        return ""
    if 0 <= number <= 0xFFFFFFFF:
        return f"!{number:08x}"
    return ""


def local_node_entry(output: str, local_node_id: str) -> dict[str, Any] | None:
    """The local node's own entry from the "Nodes in mesh" block - the
    ONE place server.py's --info consumers (telemetry, base_status)
    should read local metrics from, instead of an unbounded
    output.find(marker, node_pos) that can just as easily land inside
    the NEXT node's object. Node-id comparison is normalized the same
    way meshsrv/radio_identity.py's _normalize_node_id() is (case-
    insensitive, "!xxxxxxxx" vs a bare decimal/hex number all compare
    equal), so a case or format mismatch between the configured
    LOCAL_NODE_ID and the CLI's own JSON key can never fall through to
    "not found". Never raises; returns None if the marker is missing,
    the JSON is malformed, or no entry matches."""
    target = _normalize_node_id(local_node_id)
    if not target:
        return None
    nodes = parse_info_nodes(output)
    for node_id, node_data in nodes.items():
        if _normalize_node_id(node_id) == target and isinstance(node_data, dict):
            return node_data
    return None
