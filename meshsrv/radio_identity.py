#!/usr/bin/env python3
"""Read-only Meshtastic radio identity detection and comparison helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from meshsrv import meshsrv

IDENTITY_MATCH = "MATCH"
IDENTITY_MISMATCH = "MISMATCH"
IDENTITY_NOT_FOUND = "NOT_FOUND"
IDENTITY_DETECTION_ERROR = "DETECTION_ERROR"
IDENTITY_NOT_CHECKED = "NOT_CHECKED"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _extract_json_block(text: str, start_pos: int) -> str | None:
    start = text.find("{", max(0, start_pos))
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def _normalize_node_id(value: Any) -> str:
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


def _find_local_node_id(output: str) -> str:
    # Current Meshtastic CLI --info output includes myNodeNum in "My info".
    for pattern in (
        r'"myNodeNum"\s*:\s*(\d+)',
        r'\bmyNodeNum\b\s*[:=]\s*(\d+)',
        r'\bMy node number\b\s*[:=]\s*(\d+)',
    ):
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match:
            node_id = _normalize_node_id(match.group(1))
            if node_id:
                return node_id

    # Compatibility fallbacks for alternate CLI formats.
    for pattern in (
        r'\bLocal node(?: ID)?\b\s*[:=]\s*(![0-9a-fA-F]{8})',
        r'\bNode ID\b\s*[:=]\s*(![0-9a-fA-F]{8})',
        r'\bOwner\b[^\n]*?(![0-9a-fA-F]{8})',
    ):
        match = re.search(pattern, output, flags=re.IGNORECASE)
        if match:
            return _normalize_node_id(match.group(1))
    return ""


def _parse_nodes(output: str) -> dict[str, Any]:
    marker = output.find("Nodes in mesh:")
    if marker < 0:
        return {}
    block = _extract_json_block(output, marker)
    if not block:
        return {}
    try:
        value = json.loads(block)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_radio_identity(output: str, serial_port: str = "") -> dict[str, str]:
    """Extract the local radio identity from Meshtastic CLI --info output."""
    output = str(output or "")
    node_id = _find_local_node_id(output)
    nodes = _parse_nodes(output)
    node_data = nodes.get(node_id, {}) if node_id else {}
    if not isinstance(node_data, dict):
        node_data = {}
    user = node_data.get("user") if isinstance(node_data.get("user"), dict) else {}

    return {
        "node_id": node_id,
        "long_name": str(user.get("longName") or "").strip(),
        "short_name": str(user.get("shortName") or "").strip(),
        "hardware": str(user.get("hwModel") or "").strip(),
        "role": str(user.get("role") or "").strip(),
        "port": str(serial_port or "").strip(),
    }


def compare_radio_identity(saved_radio: Mapping[str, Any], detected_radio: Mapping[str, Any]) -> str:
    saved_id = _normalize_node_id(saved_radio.get("node_id"))
    detected_id = _normalize_node_id(detected_radio.get("node_id"))
    if not detected_id:
        return IDENTITY_NOT_FOUND
    if not saved_id:
        return IDENTITY_NOT_CHECKED
    return IDENTITY_MATCH if saved_id == detected_id else IDENTITY_MISMATCH


def detect_radio_identity(cli_path: str, serial_port: str, timeout: int = 25) -> tuple[dict[str, Any], str]:
    """Run one read-only --info probe and return (verification result, raw output)."""
    checked_at = utc_now_iso()
    configured = {"port": str(serial_port or "").strip()}
    try:
        completed = meshsrv.get_info(cli_path, serial_port=serial_port, timeout=timeout)
        output = (completed.stdout or "") + (completed.stderr or "")
        if completed.returncode != 0:
            message = output.strip() or f"Meshtastic CLI exited with code {completed.returncode}"
            return ({
                "status": IDENTITY_DETECTION_ERROR,
                "checked_at": checked_at,
                "configured": configured,
                "detected": {},
                "error": message[-1000:],
            }, output)
        detected = parse_radio_identity(output, serial_port)
        status = IDENTITY_MATCH if detected.get("node_id") else IDENTITY_NOT_FOUND
        return ({
            "status": status,
            "checked_at": checked_at,
            "configured": configured,
            "detected": detected,
            "error": None if detected.get("node_id") else "Local radio node ID was not found in Meshtastic --info output",
        }, output)
    except Exception as error:
        return ({
            "status": IDENTITY_DETECTION_ERROR,
            "checked_at": checked_at,
            "configured": configured,
            "detected": {},
            "error": str(error),
        }, "")
