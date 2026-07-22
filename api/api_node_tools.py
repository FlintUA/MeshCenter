from flask import jsonify, request
import glob
import os
import re
import subprocess
import threading
import time


_POSITION_RE = re.compile(
    r"Position received:\s*"
    r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)"
    r"(?:\s*([+-]?\d+(?:\.\d+)?)\s*m)?",
    re.IGNORECASE,
)

_PRECISION_RE = re.compile(
    r"precision\s*:\s*(\d+)",
    re.IGNORECASE,
)


def _parse_position_output(output):
    """Parse Meshtastic CLI position output."""
    text = str(output or "")
    match = _POSITION_RE.search(text)

    if not match:
        return None

    latitude = float(match.group(1))
    longitude = float(match.group(2))

    altitude = None
    if match.group(3) is not None:
        altitude_value = float(match.group(3))
        altitude = int(altitude_value) if altitude_value.is_integer() else altitude_value

    precision_match = _PRECISION_RE.search(text)
    precision = int(precision_match.group(1)) if precision_match else None

    precision_label = (
        "full"
        if re.search(r"full\s+precision", text, re.IGNORECASE)
        else None
    )

    return {
        "latitude": latitude,
        "longitude": longitude,
        "altitude": altitude,
        "precision": precision,
        "precision_label": precision_label,
        "updated": time.time(),
        "updated_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
    }


def _resolve_serial_port(configured_port):
    """Return a usable serial device, or None to let Meshtastic auto-detect it."""
    configured = str(configured_port or "").strip()

    if configured and os.path.exists(configured):
        return configured

    candidates = []
    for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        candidates.extend(glob.glob(pattern))

    candidates = sorted(set(candidates))
    return candidates[0] if candidates else None


def _friendly_command_error(action_title, output, configured_port, resolved_port):
    """Convert verbose Meshtastic/serial errors into a concise UI message."""
    text = str(output or "").strip()
    lower = text.lower()

    missing_serial = (
        "file not found error" in lower
        or "serial device" in lower and "not found" in lower
        or "no such file or directory" in lower
        or "could not open port" in lower
    )

    if missing_serial:
        if resolved_port:
            return (
                "radio_connection_failed",
                f"{action_title} could not open the radio connection on {resolved_port}. "
                "Check that the node is connected and that MeshCenter has permission to use the port.",
            )

        configured_note = (
            f" Configured port {configured_port} is unavailable."
            if configured_port else ""
        )
        return (
            "radio_not_found",
            f"{action_title} could not find a connected Meshtastic radio.{configured_note} "
            "Check the USB connection or update MESHTASTIC_PORT in config.py.",
        )

    if "permission denied" in lower:
        return (
            "radio_permission_denied",
            f"{action_title} cannot access the Meshtastic serial port. "
            "Check the Linux device permissions for the MeshCenter service user.",
        )

    if "timed out" in lower or "timeout" in lower:
        return (
            "radio_timeout",
            f"{action_title} timed out while waiting for the radio or remote node.",
        )

    return (
        "command_failed",
        f"{action_title} failed. See System Log for technical details.",
    )


def register_node_tools_routes(
    app,
    handle_errors,
    is_valid_node_id,
    nodes,
    state_lock,
    save_nodes,
    MESHTASTIC_CMD,
    MESHTASTIC_PORT,
    radio_lock,
    pause_listen,
    prepare_radio_command,
    log_system_event,
):
    """Safe, whitelisted Meshtastic commands for a selected node."""
    node_tools_lock = threading.Lock()

    @app.route("/api/node_tools", methods=["POST"])
    @handle_errors
    def api_node_tools():
        data = request.get_json(force=True) or {}

        action = str(data.get("action", "")).strip()
        node_id = str(data.get("node_id", "")).strip()

        allowed_actions = {
            "request_telemetry",
            "request_position",
            "traceroute",
        }

        if action not in allowed_actions:
            return jsonify({"ok": False, "error": "Unsupported node action"}), 400

        if not node_id or not is_valid_node_id(node_id):
            return jsonify({"ok": False, "error": "Invalid node_id"}), 400

        if not node_tools_lock.acquire(blocking=False):
            return jsonify({
                "ok": False,
                "status": "busy",
                "error": "Another Node Tools command is already running",
            }), 409

        with state_lock:
            node = dict(nodes.get(node_id, {}))

        node_name = node.get("name") or node.get("clean_name") or node_id

        resolved_port = _resolve_serial_port(MESHTASTIC_PORT)
        port_args = ["--port", resolved_port] if resolved_port else []

        if action == "traceroute":
            cmd = [
                MESHTASTIC_CMD,
                *port_args,
                "--traceroute", node_id,
                "--timeout", "30",
            ]
            action_title = "Traceroute"
            action_started_event = "Traceroute started"
            action_completed_event = "Traceroute completed"
            action_failed_event = "Traceroute failed"

        elif action == "request_telemetry":
            cmd = [
                MESHTASTIC_CMD,
                *port_args,
                "--dest", node_id,
                "--request-telemetry",
                "--timeout", "30",
            ]
            action_title = "Telemetry request"
            action_started_event = "Telemetry request started"
            action_completed_event = "Telemetry request completed"
            action_failed_event = "Telemetry request failed"

        else:
            cmd = [
                MESHTASTIC_CMD,
                *port_args,
                "--dest", node_id,
                "--request-position",
                "--timeout", "30",
            ]
            action_title = "Position request"
            action_started_event = "Position request started"
            action_completed_event = "Position request completed"
            action_failed_event = "Position request failed"

        log_system_event(
            "ACTION", "node_tools", action_started_event,
            f"Target: {node_name} ({node_id})",
        )

        print(f"[NODE TOOLS] {action_title}: {node_name} ({node_id})", flush=True)

        try:
            if not prepare_radio_command(resolved_port, timeout=10):
                log_system_event(
                    "ERROR", "node_tools", action_failed_event,
                    f"Serial port is busy: {resolved_port or 'auto-detect'}",
                )
                return jsonify({
                    "ok": False,
                    "error": "The radio connection is busy. Try again in a few seconds.",
                    "error_code": "radio_busy",
                    "technical_error": f"Serial port busy: {resolved_port or 'auto-detect'}",
                }), 503

            start_time = time.time()

            with radio_lock:
                print(f"[NODE TOOLS CMD] {cmd}", flush=True)
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=70,
                )

            elapsed = time.time() - start_time
            print(f"[NODE TOOLS] Command finished in {elapsed:.1f}s", flush=True)

            combined_output = (result.stdout or "").strip()
            print(f"[NODE TOOLS] Return code: {result.returncode}", flush=True)
            print(f"[NODE TOOLS] Output: {combined_output[:2000]}", flush=True)

            if result.returncode != 0:
                error_text = combined_output or f"{action_title} failed"
                log_system_event(
                    "ERROR", "node_tools", action_failed_event,
                    f"Target: {node_name} ({node_id}); {error_text[:500]}",
                )
                error_code, user_message = _friendly_command_error(
                    action_title,
                    error_text,
                    MESHTASTIC_PORT,
                    resolved_port,
                )
                return jsonify({
                    "ok": False,
                    "action": action,
                    "node_id": node_id,
                    "error": user_message,
                    "error_code": error_code,
                    "technical_error": error_text[-2000:],
                    "returncode": result.returncode,
                }), 500

            position = None

            if action == "request_position":
                position = _parse_position_output(combined_output)

                if position:
                    with state_lock:
                        target_node = nodes.setdefault(node_id, {
                            "node_id": node_id,
                            "name": node_name,
                        })
                        target_node["position"] = position

                    save_nodes()

                    log_system_event(
                        "OK", "node_tools", "Position saved",
                        f"Target: {node_name} ({node_id}); "
                        f"{position['latitude']}, {position['longitude']}",
                    )
                else:
                    log_system_event(
                        "WARNING", "node_tools", "Position response not parsed",
                        f"Target: {node_name} ({node_id})",
                    )

            log_system_event(
                "OK", "node_tools", action_completed_event,
                f"Target: {node_name} ({node_id})",
            )

            response_data = {
                "ok": True,
                "action": action,
                "node_id": node_id,
                "node_name": node_name,
                "message": f"{action_title} completed",
                "output": combined_output[-4000:],
                "returncode": result.returncode,
            }

            if action == "request_position":
                response_data["position"] = position
                response_data["position_saved"] = position is not None

            return jsonify(response_data)

        except subprocess.TimeoutExpired as error:
            print(f"[NODE TOOLS] Python timeout after {error.timeout}s", flush=True)
            print(f"[NODE TOOLS] Command: {' '.join(cmd)}", flush=True)

            log_system_event(
                "WARNING", "node_tools", f"{action_title} timed out",
                f"Target: {node_name} ({node_id}); Python timeout after {error.timeout}s",
            )

            return jsonify({
                "ok": False,
                "status": "timeout",
                "action": action,
                "node_id": node_id,
                "error": f"{action_title} timed out",
            }), 504

        except Exception as error:
            log_system_event(
                "ERROR", "node_tools", action_failed_event,
                f"Target: {node_name} ({node_id}); {error}",
            )

            error_code, user_message = _friendly_command_error(
                action_title,
                str(error),
                MESHTASTIC_PORT,
                resolved_port,
            )
            return jsonify({
                "ok": False,
                "action": action,
                "node_id": node_id,
                "error": user_message,
                "error_code": error_code,
                "technical_error": str(error),
            }), 500

        finally:
            time.sleep(2)
            pause_listen.clear()

            if node_tools_lock.locked():
                node_tools_lock.release()

            print("[NODE TOOLS] Listener resumed", flush=True)
            