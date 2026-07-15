from flask import jsonify, request
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

        if action == "traceroute":
            cmd = [
                MESHTASTIC_CMD,
                "--port", MESHTASTIC_PORT,
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
                "--port", MESHTASTIC_PORT,
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
                "--port", MESHTASTIC_PORT,
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
            if not prepare_radio_command(MESHTASTIC_PORT, timeout=10):
                log_system_event(
                    "ERROR", "node_tools", action_failed_event,
                    f"Serial port is busy: {MESHTASTIC_PORT}",
                )
                return jsonify({
                    "ok": False,
                    "error": f"Serial port busy: {MESHTASTIC_PORT}",
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
                return jsonify({
                    "ok": False,
                    "action": action,
                    "node_id": node_id,
                    "error": error_text,
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

            return jsonify({
                "ok": False,
                "action": action,
                "node_id": node_id,
                "error": str(error),
            }), 500

        finally:
            time.sleep(2)
            pause_listen.clear()

            if node_tools_lock.locked():
                node_tools_lock.release()

            print("[NODE TOOLS] Listener resumed", flush=True)
            