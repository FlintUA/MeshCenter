"""General helper functions for MeshCenter."""

import re
import time


_device_model_cache = None


def get_device_model():
    """Best-effort short hardware model string. Generic across Raspberry Pi
    and non-Pi ARM/Linux hosts (e.g. Droidian phones) - reads the same
    /proc/device-tree/model source api_system_info() already uses for the
    System Information card, so both surfaces agree. Cached: hardware
    can't change mid-process."""
    global _device_model_cache
    if _device_model_cache is not None:
        return _device_model_cache
    model = ""
    try:
        with open("/proc/device-tree/model", "r") as f:
            model = f.read().replace("\x00", "").strip()
    except OSError:
        pass
    if not model:
        try:
            import platform
            model = platform.node() or ""
        except Exception:
            model = ""
    _device_model_cache = model
    return _device_model_cache


def now():
    return time.strftime("%H:%M:%S")


def timestamp_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def voltage_to_percent(voltage):
    try:
        v = float(voltage)
        if v >= 4.20:
            return 100
        if v >= 4.15:
            return 95
        if v >= 4.10:
            return 90
        if v >= 4.05:
            return 85
        if v >= 4.00:
            return 80
        if v >= 3.95:
            return 70
        if v >= 3.90:
            return 60
        if v >= 3.85:
            return 50
        if v >= 3.80:
            return 40
        if v >= 3.75:
            return 30
        if v >= 3.70:
            return 20
        if v >= 3.60:
            return 10
        return 0
    except Exception:
        return None


def node_num_to_id(num):
    try:
        hex_str = format(int(num) & 0xFFFFFFFF, "08x")
        return "!" + hex_str
    except Exception:
        return ""


def normalize_node_id(node_id):
    if not node_id:
        return None
    if node_id.startswith("!") and len(node_id) == 9:
        return node_id
    if node_id.startswith("!1p"):
        hex_part = node_id[3:]
        if len(hex_part) == 8:
            return "!" + hex_part
    if re.match(r"^[0-9a-fA-F]{8}$", node_id):
        return "!" + node_id
    if node_id.startswith("!") and len(node_id) != 9:
        hex_part = re.search(r"[0-9a-fA-F]{8}", node_id)
        if hex_part:
            return "!" + hex_part.group(0)
    return node_id


def normalize_node_id_with_aliases(node_id):
    if not node_id:
        return None
    return normalize_node_id(node_id)


_JSON_SAFE_SCALARS = (str, int, float, bool)


def json_safe(value):
    """Recursively strips anything json.dumps() can't serialize, keeping
    dict/list/tuple structure and JSON-primitive leaves untouched.

    Written for adapters/meshtastic/*.py's NodeInfo.position conversion:
    the meshtastic library's own node dict sometimes carries a raw,
    non-serializable protobuf message object under a "raw" key (see the
    installed library's meshtastic/__init__.py, KnownProtocol.protobufFactory
    handling in _handlePacketFromRadio - "Also provide the protobuf raw",
    consumed by _onPositionReceive) - that object then flows untouched
    through NodeInfo.position into meshsrv/ipc_protocol.py's IPC response
    dict, where json.dumps() previously raised uncaught and crashed the
    whole adapter subprocess (adapters/meshtastic/ipc_server.py's
    serve_forever() - see that function's own try/except around the write
    for the second, last-resort layer of this fix).

    Deliberately generic rather than hardcoded to the one "raw" key, so
    any other non-JSON-safe value a library upgrade injects degrades the
    same way (silently dropped) instead of crashing the IPC channel
    again. Never raises: an unrepresentable top-level value becomes None,
    an unrepresentable dict/list member is simply omitted rather than
    replacing the whole structure."""
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
