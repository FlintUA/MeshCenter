"""Shared JSON-safety sanitization for every RadioTransport implementation
in this package - specifically, NodeInfo.position (meshsrv/radio_transport.py),
which is where this was needed (see below), but written generically rather
than hardcoded to that one field.

Lives inside adapters/meshtastic/, not utils/helpers.py, deliberately:
scripts/build-release.sh packages the Meshtastic adapter as a standalone
archive against an explicit, hand-audited transitive import whitelist
(ADAPTER_WHITELIST) - Core-side utils/ is not part of it, and nothing
outside this package currently needs json_safe(). A function only this
package's own transports call belongs inside it, the same reasoning
_timeout_support.py already follows for the shared watchdog logic.

LIVE-CAUGHT BUG this exists to fix: the installed meshtastic library
injects a raw, non-JSON-serializable protobuf message object under
position["raw"] for any node with a live GPS fix (see the installed
library's meshtastic/__init__.py, _handlePacketFromRadio's
`asDict["decoded"][handler.name]["raw"] = pb` - "Also provide the
protobuf raw", a deliberate library feature - consumed as-is by
_onPositionReceive's `node["position"] = p`). That object then flowed
untouched through NodeInfo.position into meshsrv/ipc_protocol.py's IPC
response dict, where adapters/meshtastic/ipc_server.py's serve_forever()
crashed with an uncaught TypeError on json.dumps(response) - outside
_AdapterDispatcher.handle()'s own try/except, which only guards a
*raised* exception, not a successful result that still isn't
representable - taking down the whole adapter subprocess over one bad
field. serve_forever() itself also wraps that json.dumps() call as a
last-resort layer independent of this one - see its own comment.

Confirmed telemetry's own equivalent "raw" protobuf injection
(deviceMetrics/environmentMetrics/powerMetrics) does NOT have the same
exposure: the library only merges the specific per-metric sub-dict into
the node, never the top-level decoded dict "raw" sits on - position is
the one field genuinely affected, per each transport's own
_to_node_info().
"""
from __future__ import annotations

_JSON_SAFE_SCALARS = (str, int, float, bool)


def json_safe(value):
    """Recursively strips anything json.dumps() can't serialize, keeping
    dict/list/tuple structure and JSON-primitive leaves untouched.

    Deliberately generic rather than hardcoded to the one "raw" key, so
    any other non-JSON-safe value a future library upgrade injects
    degrades the same way (silently dropped) instead of crashing the IPC
    channel again. Never raises: an unrepresentable top-level value
    becomes None, an unrepresentable dict/list member is simply omitted
    rather than replacing the whole structure."""
    if value is None or isinstance(value, _JSON_SAFE_SCALARS):
        return value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items() if _is_json_safe_shape(v)}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value if _is_json_safe_shape(v)]
    return None


def _is_json_safe_shape(value):
    """True if `value` is a type json_safe() knows how to handle (a JSON
    primitive, None, or a container) - decides whether to keep or drop a
    dict/list member; nested members are recursively sanitized by
    json_safe() itself, not fully validated here."""
    return value is None or isinstance(value, _JSON_SAFE_SCALARS) or isinstance(value, (dict, list, tuple))
