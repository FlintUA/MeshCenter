#!/usr/bin/env python3
"""Read-only Meshtastic radio identity detection and comparison helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping

from meshsrv import meshtastic_transport
from meshsrv.radio_transport import ConnectionDescriptor, ConnectionType, RadioTransport, TransportError
from meshsrv.runtime_identity import discover_serial_ports

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


def _parse_metadata(output: str) -> dict[str, Any]:
    # `meshtastic --info` prints a separate "Metadata: {...}" line (from
    # MeshInterface.showInfo(), see mesh_interface.py) holding the local
    # node's DeviceMetadata - firmwareVersion lives here, not in the
    # per-node "Nodes in mesh" block used for name/hardware/role above.
    marker = output.find("Metadata:")
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
    metadata = _parse_metadata(output)

    return {
        "node_id": node_id,
        "long_name": str(user.get("longName") or "").strip(),
        "short_name": str(user.get("shortName") or "").strip(),
        "hardware": str(user.get("hwModel") or "").strip(),
        "role": str(user.get("role") or "").strip(),
        "port": str(serial_port or "").strip(),
        "firmware_version": str(metadata.get("firmwareVersion") or "").strip(),
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
        completed = meshtastic_transport.get_info(cli_path, serial_port=serial_port, timeout=timeout)
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

def detect_connected_radio(
    cli_path: str,
    preferred_port: str = "",
    timeout_per_port: int = 35,
    settle_seconds: float = 1.5,
) -> dict[str, Any]:
    """Probe available serial ports and return the first detected local radio.

    The preferred port is tried first, followed by persistent by-id links and
    ttyACM/ttyUSB devices. Errors are returned for diagnostics instead of being
    discarded.
    """
    import os
    import time

    preferred = str(preferred_port or "").strip()
    ports: list[str] = []
    seen_real: set[str] = set()

    for candidate in ([preferred] if preferred else []) + discover_serial_ports():
        candidate = str(candidate or "").strip()
        if not candidate or not os.path.exists(candidate):
            continue
        real = os.path.realpath(candidate)
        if real in seen_real:
            continue
        seen_real.add(real)
        ports.append(candidate)

    if settle_seconds > 0:
        time.sleep(settle_seconds)

    attempts: list[dict[str, Any]] = []
    for port in ports:
        result, _ = detect_radio_identity(
            cli_path,
            port,
            timeout=max(10, int(timeout_per_port)),
        )
        detected = dict(result.get("detected") or {})
        attempts.append({
            "port": port,
            "status": result.get("status"),
            "error": result.get("error"),
        })
        if detected.get("node_id"):
            detected["port"] = port
            return {
                "ok": True,
                "detected": detected,
                "checked_at": result.get("checked_at"),
                "port": port,
                "attempts": attempts,
                "candidates": ports,
            }

    return {
        "ok": False,
        "detected": {},
        "checked_at": utc_now_iso(),
        "port": "",
        "attempts": attempts,
        "candidates": ports,
        "error": (
            "No Meshtastic radio identity could be read from the available "
            "serial ports."
            if ports else
            "No serial radio ports were found."
        ),
    }


def _tcp_identity_from_connected_transport(
    transport: RadioTransport,
    host: str,
    port: int,
    timeout: float,
) -> dict[str, Any]:
    """Reads node_id/long_name/short_name/hardware/role/firmware_version
    from a TCP transport that's ALREADY connected - shared by
    detect_tcp_radio_identity() (connects itself first) and
    fetch_connected_tcp_identity() (assumes the caller already holds a
    live connection, e.g. api/api_meshtastic.py's post-connect identity
    check right after transport_router.switch() succeeds - calling
    connect() a second time there would be redundant at best and is not
    this function's job). Raises TransportError straight through on a
    failed get_local_node() - matches that method's own contract,
    callers decide what a failed read means for their own flow."""
    local_node = transport.get_local_node(timeout=timeout)
    user = local_node.user
    firmware_version = ""
    try:
        metadata = transport.get_metadata(timeout=timeout)
        metadata_json = json.loads(metadata.get("metadata_json") or "{}")
        firmware_version = str(metadata_json.get("firmwareVersion") or "").strip()
    except Exception:
        # Metadata is a best-effort extra, never fatal to identity
        # detection itself - a radio that answers get_local_node() but
        # not get_metadata() cleanly still has a confirmed node_id.
        pass

    return {
        "node_id": local_node.node_id,
        "long_name": user.long_name if user else "",
        "short_name": user.short_name if user else "",
        "hardware": user.hw_model if user else "",
        # NodeUser (meshsrv/radio_transport.py) carries no `role` field at
        # all - neither BLETransport's nor SerialTransport's own
        # _to_node_info() populate one either (confirmed by reading both).
        # Kept as an always-empty key purely so this dict has the same
        # shape as detect_radio_identity()'s (CLI-sourced, which does have
        # a real "role" value) - callers that read radio.get("role", "")
        # keep working unchanged, just never populated for TCP.
        "role": "",
        "port": "",
        "host": host,
        "tcp_port": int(port) if port else 0,
        "firmware_version": firmware_version,
    }


def fetch_connected_tcp_identity(
    transport: RadioTransport,
    host: str,
    port: int,
    timeout: float = 15,
) -> dict[str, Any]:
    """Public entry point for a caller that already holds a live TCP
    connection (unlike detect_tcp_radio_identity(), which connects
    itself) and just wants to read what's actually on the other end -
    e.g. api/api_meshtastic.py's post-connect identity check, run right
    after transport_router.switch() has already established the link
    (Radio TCP Transport part 2 correction pass #4: "Settings -> TCP
    Connect" must verify the connected radio's real identity against
    the accepted one, not just persist transport/endpoint blindly).
    Raises TransportError on a failed read - the caller is already in
    its own try/except around this exact call, unlike
    detect_tcp_radio_identity()'s boot-time/discovery callers, which
    want a squashed result dict instead."""
    return _tcp_identity_from_connected_transport(transport, host, port, timeout)


def detect_tcp_radio_identity(
    transport: RadioTransport,
    host: str,
    port: int,
    timeout: float = 25,
) -> tuple[dict[str, Any], str]:
    """TCP counterpart to detect_radio_identity() - same return shape
    ((result_dict, raw_output_str)), so verify_radio_identity() and the
    node-manager profile routes in server.py don't need two different
    shapes to handle. Detection here goes through the real
    RadioTransport/AdapterIPCTransport boundary (a live connect() +
    get_local_node()/get_metadata(), via the adapter subprocess) rather
    than shelling out to a CLI - there is no `meshtastic --host ...
    --info` equivalent this project's Meshtastic CLI dependency provides
    the way `--port ... --info` does for serial (see
    meshsrv/meshtastic_transport.py), so TCP identity detection has
    always gone through the same transport boundary the rest of Core's
    TCP traffic uses, never a second, parallel CLI-based path.

    `result_dict["error_code"]` is a new, additive field (always None on
    the serial detect_radio_identity() path above) carrying the raw
    TransportErrorCode value (dns_error/connect_refused/connect_timeout/
    protocol_sync_timeout/tcp_connected/remote_disconnect/...) so a
    caller/UI can surface PR #277's specific diagnostic codes instead of
    a squashed generic "detection failed" message.

    Does not leave the connection open on failure - the caller
    (verify_radio_identity() at startup, or a node-manager profile route)
    decides separately whether to keep or discard this connection.
    Deliberately does NOT close it on success either: identity detection
    for TCP unavoidably *is* the same connect a caller may want to keep
    using afterward (unlike serial's one-shot `--info` CLI probe, which
    is process-isolated from the real, separate --listen subprocess) -
    the caller is responsible for deciding whether to disconnect().
    """
    checked_at = utc_now_iso()
    host = str(host or "").strip()
    configured = {"host": host, "port": int(port) if port else 0}

    if not host:
        return ({
            "status": IDENTITY_DETECTION_ERROR,
            "checked_at": checked_at,
            "configured": configured,
            "detected": {},
            "error": "No TCP host configured",
            "error_code": None,
        }, "")

    descriptor = ConnectionDescriptor(type=ConnectionType.TCP, address=f"{host}:{int(port) if port else 0}")
    try:
        transport.connect(descriptor, timeout=timeout)
        detected = _tcp_identity_from_connected_transport(transport, host, port, timeout)
    except TransportError as error:
        return ({
            "status": IDENTITY_DETECTION_ERROR,
            "checked_at": checked_at,
            "configured": configured,
            "detected": {},
            "error": str(error),
            "error_code": error.code.value,
        }, "")

    status = IDENTITY_MATCH if detected.get("node_id") else IDENTITY_NOT_FOUND
    return ({
        "status": status,
        "checked_at": checked_at,
        "configured": configured,
        "detected": detected,
        "error": None if detected.get("node_id") else "TCP radio responded but reported no node ID",
        "error_code": None,
    }, "")

