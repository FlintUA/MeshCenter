"""
MeshCenter Schedule Actions
Executes action types: log_entry, mesh_send, send_data_report.

send_mesh_message() and _get_field_value() need access to server.py's
shared `nodes` dict / `state_lock` / radio primitives. Per R3 (see
schedule_engine.start()'s docstring), meshsrv/*.py modules never import
server.py directly (server.py imports FROM meshsrv/*, so that would risk a
circular import) - the references are handed in once via configure(),
called from schedule_engine.start(), which itself receives them from
server.py at startup. Until configure() has been called (should never
happen outside of tests - server.py always calls schedule_engine.start()
with real references), the module-level globals stay None and every
function here degrades to a safe no-op/failure instead of crashing.

Task 44: send_mesh_message() no longer opens its own
meshtastic.serial_interface.SerialInterface - it sends through the
injected `radio_transport` (a RadioTransport instance, see
meshsrv/radio_transport.py) instead. This module now imports no
`meshtastic` symbol at all.
"""
import time
from system_log import log_system_event

_nodes = None
_state_lock = None
_radio_transport = None
_is_radio_available = None
_LOCAL_NODE_ID = None
_add_message = None
_LOCAL_NODE_NAME = None
_CHANNEL_CHAT_ID = None


def configure(nodes=None, state_lock=None, radio_transport=None,
              is_radio_available=None, LOCAL_NODE_ID=None,
              add_message=None, LOCAL_NODE_NAME=None, CHANNEL_CHAT_ID=None):
    """Called once from meshsrv.schedule_engine.start() with references
    injected from server.py. See this module's docstring."""
    global _nodes, _state_lock, _radio_transport
    global _is_radio_available, _LOCAL_NODE_ID
    global _add_message, _LOCAL_NODE_NAME, _CHANNEL_CHAT_ID
    _nodes = nodes
    _state_lock = state_lock
    _radio_transport = radio_transport
    _is_radio_available = is_radio_available
    _LOCAL_NODE_ID = LOCAL_NODE_ID
    _add_message = add_message
    _LOCAL_NODE_NAME = LOCAL_NODE_NAME
    _CHANNEL_CHAT_ID = CHANNEL_CHAT_ID


def run_action(action: dict, rule: dict):
    t = action.get('type')
    if t == 'log_entry':
        _do_log_entry(rule)
    elif t == 'mesh_send':
        _do_mesh_send(action, rule)
    elif t == 'send_data_report':
        _do_send_data_report(action, rule)
    else:
        print(f"[Schedule] Unknown action type: {t}", flush=True)


def _do_log_entry(rule: dict):
    log_system_event(
        title=f"Schedule: {rule.get('label', rule.get('id'))}",
        details="Schedule executed",
        level='INFO',
        source='schedule_engine'
    )


def send_mesh_message(message: str, target_type: str, node_id: str, channel_index: int) -> bool:
    """Send a static text message to mesh.

    Task 44: sends through the injected RadioTransport's send_text()
    instead of opening its own meshtastic.serial_interface.SerialInterface
    - same destination/want_ack/channel_index selection logic as before,
    same "no traceback, no import here" boundary described in the module
    docstring. Still an independent send path from api/api_chat.py's own
    send worker (that one is not migrated until Task 46 - see the Task 44
    investigation report) - both will call through the same transport once
    that migration lands, removing the duplication, not before.
    """
    if _radio_transport is None:
        print("[Schedule] send_mesh_message: not configured (schedule_actions.configure() "
              "was never called with a radio_transport)", flush=True)
        return False

    if _is_radio_available is not None and not _is_radio_available():
        print("[Schedule] send_mesh_message: radio released for external configuration", flush=True)
        return False

    text = str(message or "").strip()
    if not text:
        print("[Schedule] send_mesh_message: empty message, skipped", flush=True)
        return False

    if target_type == 'node' and node_id:
        destination = node_id
        want_ack = True
        channel_idx = 0
    else:
        destination = "^all"
        want_ack = False
        try:
            channel_idx = int(channel_index or 0)
        except (TypeError, ValueError):
            channel_idx = 0

    from meshsrv.radio_transport import OutgoingMessage

    try:
        result = _radio_transport.send_text(
            OutgoingMessage(
                text=text,
                destination_id=destination,
                channel_index=channel_idx,
                want_ack=want_ack,
            ),
            timeout=10,
        )
        if not result.accepted:
            print(f"[Schedule] send_mesh_message: send failed: {result.error}", flush=True)
            return False

        print(
            f"[Schedule] Mesh message sent: destination={destination}, "
            f"channel_index={channel_idx}, want_ack={want_ack}",
            flush=True
        )

        # Mirror api/api_chat.py's send worker (api/api_chat.py:117-153):
        # a successful radio send alone leaves no trace in the local
        # chat history, so a schedule/timer-triggered message was
        # invisible in the MeshCenter chat UI even though it went
        # out over the mesh. add_message/LOCAL_NODE_NAME/
        # CHANNEL_CHAT_ID are injected via configure() (see this
        # module's docstring and schedule_engine.start()) - they are
        # the actual server.py function/globals, so calling
        # add_message() here writes into server.py's own
        # messages / chats state exactly as if api_chat.py had done it.
        if _add_message is not None and _state_lock is not None:
            try:
                if target_type == 'node' and node_id:
                    history_chat_id = node_id
                else:
                    history_chat_id = (
                        _CHANNEL_CHAT_ID if channel_idx == 0
                        else f"channel:{channel_idx}"
                    )
                with _state_lock:
                    _add_message(
                        "me",
                        _LOCAL_NODE_NAME,
                        text,
                        node_id=_LOCAL_NODE_ID,
                        chat_id=history_chat_id,
                    )
            except Exception as history_error:
                print(f"[Schedule] send_mesh_message: local chat-history write "
                      f"failed (message was still sent over mesh): {history_error}",
                      flush=True)

        return True
    except Exception as e:
        print(f"[Schedule] send_mesh_message: error: {e}", flush=True)
        return False


def _do_mesh_send(action: dict, rule: dict):
    p = action.get('params', {})
    send_mesh_message(
        message=p.get('message', ''),
        target_type=p.get('target_type', 'node'),
        node_id=p.get('node_id', ''),
        channel_index=p.get('channel_index', 0)
    )


def _do_send_data_report(action: dict, rule: dict):
    p = action.get('params', {})
    source_node = p.get('source_node', '')
    fields = p.get('fields', [])
    fmt = p.get('format', 'compact')
    policy = p.get('stale_policy', 'send_with_age')
    threshold_s = p.get('stale_threshold_min', 30) * 60

    now = int(time.time())
    parts = []
    oldest_ts = now

    for field in fields:
        value, ts = _get_field_value(field, source_node)
        if value is not None:
            parts.append(_format_field(field, value, fmt))
            if ts:
                oldest_ts = min(oldest_ts, ts)

    if not parts:
        print("[Schedule] send_data_report: no data available", flush=True)
        return

    message = _join_parts(parts, fmt)
    age_s = now - oldest_ts

    if age_s > threshold_s:
        age_label = _format_age(age_s)
        message = f"{message}  [{age_label}]" if fmt == 'compact' else f"{message}\n— data {age_label} —"
    elif policy == 'skip_if_stale':
        from meshsrv.notification_service import push_notification
        push_notification(
            level='warning', source='schedule_engine',
            title=rule.get('label', 'Schedule'), body="Data is stale, send skipped"
        )
        return

    message = _truncate_bytes(message, 190)
    send_mesh_message(
        message=message,
        target_type=p.get('target_type', 'channel'),
        node_id=p.get('node_id', ''),
        channel_index=p.get('channel_index', 0)
    )


# Field name -> which per-node metrics sub-dict server.py's
# apply_node_telemetry() (server.py:1395-1461) stores it under. Confirmed by
# reading that function directly (R1): it merges parsed telemetry into
# nodes[node_id]["device_metrics"] / ["environment_metrics"] /
# ["power_metrics"], each stamped with an "updated" epoch-seconds timestamp
# and a "source" field (server.py:1416-1433). There is no separate getter
# function for "current value of field X for node Y" - server.py's own
# /api/telemetry/history route (server.py:5305 area) reads the same shared
# `nodes` dict / telemetry.telemetry_history list directly under
# `state_lock` rather than through an accessor, so reaching into `nodes`
# under `state_lock` here matches the established pattern rather than
# inventing a new one.
_FIELD_GROUP = {
    "temperature": "environment_metrics",
    "humidity": "environment_metrics",
    "pressure": "environment_metrics",
    "voltage": "power_metrics",
    "current": "power_metrics",
    "power": "power_metrics",
    "battery_level": "device_metrics",
    "channel_utilization": "device_metrics",
    "air_util_tx": "device_metrics",
    "uptime_seconds": "device_metrics",
}


def _get_field_value(field: str, source_node: str):
    """Return (value, timestamp) for a telemetry field of a given node.

    Reads server.py's shared `nodes` dict (injected via configure(), see
    R3/R1) under `state_lock`, exactly like server.py's own routes do.
    Returns (None, None) if schedule_actions hasn't been configured, the
    node is unknown, or the field has never been recorded.
    """
    if _nodes is None or _state_lock is None:
        print("[Schedule] _get_field_value: not configured (nodes/state_lock unavailable)", flush=True)
        return None, None

    node_id = source_node or _LOCAL_NODE_ID
    if not node_id:
        return None, None

    group_name = _FIELD_GROUP.get(field)

    with _state_lock:
        node = _nodes.get(node_id)
        if not isinstance(node, dict):
            return None, None

        value = None
        ts = None
        if group_name:
            group = node.get(group_name)
            if isinstance(group, dict):
                value = group.get(field)
                ts = group.get('updated')

        # Fall back to the flat top-level copy apply_node_telemetry() also
        # writes onto the node dict (server.py:1408-1410) - covers fields
        # queried before the grouped sub-dict was ever populated, or fields
        # not in _FIELD_GROUP.
        if value is None:
            value = node.get(field)
        if ts is None:
            ts = node.get('last_telemetry_time')

        return value, ts


def _format_field(field: str, value, fmt: str) -> str:
    units = {'voltage': 'V', 'current': 'mA', 'power': 'mW', 'battery_level': '%',
             'temperature': '°C', 'humidity': '%', 'pressure': 'hPa'}
    u = units.get(field, '')
    if fmt == 'compact':
        return f"{value}{u}"
    label = field.replace('_', ' ').title()
    return f"{label}: {value}{u}"


def _join_parts(parts: list, fmt: str) -> str:
    return '  '.join(parts) if fmt == 'compact' else '\n'.join(parts)


def _format_age(age_s: int) -> str:
    if age_s < 3600:
        return f"{age_s // 60}min ago"
    elif age_s < 86400:
        return f"{age_s // 3600}h ago"
    return f"{age_s // 86400}d ago"


def _truncate_bytes(s: str, max_bytes: int) -> str:
    encoded = s.encode('utf-8')
    if len(encoded) <= max_bytes:
        return s
    truncated = encoded[:max_bytes - 3]
    return truncated.decode('utf-8', errors='ignore') + '…'
