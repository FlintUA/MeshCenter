"""General helper functions for MeshCenter."""

import re
import time


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


def is_valid_node_id(node_id, channel_chat_id=None):
    if not node_id:
        return False
    if channel_chat_id is not None and node_id == channel_chat_id:
        return True
    return node_id.startswith("!") and len(node_id) >= 5


def sanitize_text(text):
    if not text:
        return ""
    if len(text) > 500:
        text = text[:500]
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text


def friendly_unknown_node_name(node_id):
    if node_id and node_id.startswith("!") and len(node_id) >= 5:
        return "Meshtastic " + node_id[-4:]
    return node_id or "Unknown"
