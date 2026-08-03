from flask import jsonify, request
import glob
import os
import re
import selectors
import subprocess
import threading
import time

from meshsrv.action_engine import (
    ActionContext,
    ActionDefinition,
    ActionRegistry,
    ActionResult,
    ActionRunner,
)


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



_TELEMETRY_PATTERNS = {
    "battery_level": re.compile(r"Battery level:\s*([+-]?\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
    "voltage": re.compile(r"Voltage:\s*([+-]?\d+(?:\.\d+)?)\s*V", re.IGNORECASE),
    "channel_utilization": re.compile(
        r"(?:Total\s+)?channel utilization:\s*([+-]?\d+(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    ),
    "air_util_tx": re.compile(
        r"(?:Transmit\s+)?air utilization(?:\s+TX)?:\s*([+-]?\d+(?:\.\d+)?)\s*%",
        re.IGNORECASE,
    ),
    "uptime_seconds": re.compile(r"Uptime:\s*(\d+)\s*s", re.IGNORECASE),
    "temperature": re.compile(r"Temperature:\s*([+-]?\d+(?:\.\d+)?)\s*(?:°?\s*C)?", re.IGNORECASE),
    "humidity": re.compile(r"Humidity:\s*([+-]?\d+(?:\.\d+)?)\s*%", re.IGNORECASE),
    "pressure": re.compile(
        r"(?:Barometric\s+)?pressure:\s*([+-]?\d+(?:\.\d+)?)\s*(hPa|mbar|mmHg)?",
        re.IGNORECASE,
    ),
    "current": re.compile(r"Current:\s*([+-]?\d+(?:\.\d+)?)\s*(mA|A)?", re.IGNORECASE),
    "power": re.compile(r"Power:\s*([+-]?\d+(?:\.\d+)?)\s*(mW|W)?", re.IGNORECASE),
}


def _parse_telemetry_output(output):
    """Parse telemetry values printed by the Meshtastic CLI."""
    text = str(output or "")
    if "telemetry received" not in text.lower():
        return None

    values = {}
    for key, pattern in _TELEMETRY_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue

        raw_value = float(match.group(1))
        value = int(raw_value) if key == "uptime_seconds" else raw_value

        if key == "battery_level":
            # Meshtastic uses 101 to mean powered/external or effectively full.
            value = 100.0 if raw_value > 100 else raw_value
        elif key == "pressure":
            unit = (match.group(2) or "hPa").lower()
            if unit == "mmhg":
                # Keep the normalized node-state pressure in hPa.
                value = raw_value / 0.750061683
        elif key == "current":
            unit = (match.group(2) or "mA").lower()
            if unit == "a":
                value = raw_value * 1000.0
        elif key == "power":
            unit = (match.group(2) or "mW").lower()
            if unit == "w":
                value = raw_value * 1000.0

        values[key] = value

    if not values:
        return None

    values["updated"] = time.time()
    values["updated_time"] = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(values["updated"]),
    )
    return values


def _apply_telemetry_to_node(nodes, state_lock, node_id, node_name, telemetry):
    """Store normalized telemetry in both flat and grouped node-state fields."""
    with state_lock:
        node = nodes.setdefault(node_id, {
            "node_id": node_id,
            "name": node_name,
        })

        node.update({
            key: value
            for key, value in telemetry.items()
            if key not in {"updated", "updated_time"}
        })
        node["last_telemetry_time"] = telemetry["updated"]
        node["last_telemetry_time_text"] = telemetry["updated_time"]

        device_metrics = node.setdefault("device_metrics", {})
        for key in (
            "battery_level",
            "voltage",
            "channel_utilization",
            "air_util_tx",
            "uptime_seconds",
        ):
            if key in telemetry:
                device_metrics[key] = telemetry[key]
        device_metrics["updated"] = telemetry["updated"]

        environment_metrics = node.setdefault("environment_metrics", {})
        for key in ("temperature", "humidity", "pressure"):
            if key in telemetry:
                environment_metrics[key] = telemetry[key]
        if any(key in telemetry for key in ("temperature", "humidity", "pressure")):
            environment_metrics["updated"] = telemetry["updated"]

        power_metrics = node.setdefault("power_metrics", {})
        for key in ("voltage", "current", "power"):
            if key in telemetry:
                power_metrics[key] = telemetry[key]
        if any(key in telemetry for key in ("voltage", "current", "power")):
            power_metrics["updated"] = telemetry["updated"]

    return node

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


def _dispatch_cli_request(cmd, sent_markers, startup_timeout=15, settle_seconds=1.0):
    """Start a Meshtastic CLI request and return as soon as it is transmitted.

    The stock CLI normally waits for the remote reply while owning the serial
    port. MeshCenter instead needs to resume its listener quickly so the reply
    can be received by the normal passive telemetry pipeline.
    """
    process_env = os.environ.copy()
    process_env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="ignore",
        env=process_env,
    )

    output_lines = []
    sent = False
    deadline = time.time() + max(1, float(startup_timeout))
    selector = selectors.DefaultSelector()

    try:
        if process.stdout is None:
            raise RuntimeError("Meshtastic CLI stdout is unavailable")

        selector.register(process.stdout, selectors.EVENT_READ)

        while time.time() < deadline:
            remaining = max(0.05, deadline - time.time())
            events = selector.select(timeout=min(0.25, remaining))

            if not events:
                if process.poll() is not None:
                    break
                continue

            for key, _ in events:
                line = key.fileobj.readline()
                if not line:
                    continue
                output_lines.append(line.rstrip())
                lowered = line.lower()
                if any(marker.lower() in lowered for marker in sent_markers):
                    sent = True
                    break

            if sent:
                break

        if sent:
            # Give pyserial a brief moment to flush the already-created packet.
            time.sleep(max(0.0, float(settle_seconds)))
        elif process.poll() is None:
            output_lines.append("Request dispatch confirmation was not received")

    finally:
        try:
            selector.close()
        except Exception:
            pass

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

        if process.stdout is not None:
            try:
                remainder = process.stdout.read()
                if remainder:
                    output_lines.extend(remainder.splitlines())
            except Exception:
                pass

    return sent, process.returncode, "\n".join(output_lines).strip()


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
    wait_serial_release,
    log_system_event,
    is_radio_available,
):
    """Register Node Tools through the shared MeshCenter action engine.

    The public ``/api/node_tools`` endpoint remains compatible with the
    existing UI.  Internally, all tools now use ActionRegistry, ActionRunner
    and ActionResult, forming the first stable extension point for future
    plugin and device actions.
    """
    cli_path = str(MESHTASTIC_CMD or "").strip()
    configured_port = str(MESHTASTIC_PORT or "").strip()
    if not cli_path:
        raise RuntimeError("Node Tools received an empty Meshtastic CLI path")
    if not os.path.isfile(cli_path) or not os.access(cli_path, os.X_OK):
        raise RuntimeError(f"Meshtastic CLI is not executable: {cli_path}")
    if not configured_port:
        raise RuntimeError("Node Tools received an empty Meshtastic serial port")

    node_tools_lock = threading.Lock()
    registry = ActionRegistry()

    def build_command(action_id, node_id, resolved_port):
        port_args = ["--port", resolved_port] if resolved_port else []

        if action_id == "traceroute":
            return (
                [
                    cli_path,
                    *port_args,
                    "--traceroute", node_id,
                    "--timeout", "30",
                ],
                "Traceroute",
                "Traceroute started",
                "Traceroute completed",
                "Traceroute failed",
            )

        if action_id == "request_telemetry":
            return (
                [
                    cli_path,
                    *port_args,
                    "--dest", node_id,
                    "--request-telemetry",
                    "--timeout", "30",
                ],
                "Telemetry request",
                "Telemetry request started",
                "Telemetry request completed",
                "Telemetry request failed",
            )

        return (
            [
                cli_path,
                *port_args,
                "--dest", node_id,
                "--request-position",
                "--timeout", "30",
            ],
            "Position request",
            "Position request started",
            "Position request completed",
            "Position request failed",
        )

    def execute_node_tool(context: ActionContext) -> ActionResult:
        action = context.action.action_id
        node_id = context.node_id
        node_name = context.node_name
        resolved_port = _resolve_serial_port(configured_port)

        (
            cmd,
            action_title,
            action_started_event,
            action_completed_event,
            action_failed_event,
        ) = build_command(action, node_id, resolved_port)

        log_system_event(
            "ACTION", "node_tools", action_started_event,
            f"Target: {node_name} ({node_id}); Job: {context.job_id}",
        )
        print(
            f"[NODE TOOLS] {action_title}: {node_name} ({node_id}); "
            f"job={context.job_id}",
            flush=True,
        )

        if not prepare_radio_command(resolved_port, timeout=10):
            log_system_event(
                "ERROR", "node_tools", action_failed_event,
                f"Serial port is busy: {resolved_port or 'auto-detect'}; "
                f"Job: {context.job_id}",
            )
            return ActionResult.failure(
                "The radio connection is busy. Try again in a few seconds.",
                error_code="radio_busy",
                state="busy",
                data={"technical_error": f"Serial port busy: {resolved_port or 'auto-detect'}"},
                http_status=503,
            )

        try:
            start_time = time.time()

            # Device telemetry is dispatched without waiting for the remote
            # response. The listener resumes immediately and receives the
            # eventual TELEMETRY_APP / nodeinfo update through the normal path.
            if action == "request_telemetry":
                with radio_lock:
                    print(f"[NODE TOOLS CMD] {cmd}", flush=True)
                    dispatched, returncode, combined_output = _dispatch_cli_request(
                        cmd,
                        sent_markers=(
                            "sending device_metrics telemetry request",
                            "sending telemetry request",
                        ),
                        startup_timeout=15,
                        settle_seconds=1.0,
                    )

                elapsed = time.time() - start_time
                print(
                    f"[NODE TOOLS] Request dispatch finished in {elapsed:.1f}s; "
                    f"sent={dispatched}; job={context.job_id}",
                    flush=True,
                )
                print(f"[NODE TOOLS] Output: {combined_output[:2000]}", flush=True)

                if not dispatched:
                    error_text = combined_output or "Telemetry request was not transmitted"
                    error_code, user_message = _friendly_command_error(
                        action_title, error_text, configured_port, resolved_port,
                    )
                    log_system_event(
                        "ERROR", "node_tools", action_failed_event,
                        f"Target: {node_name} ({node_id}); Job: {context.job_id}; "
                        f"{error_text[:500]}",
                    )
                    return ActionResult.failure(
                        user_message,
                        error_code=error_code,
                        data={
                            "technical_error": error_text[-2000:],
                            "returncode": returncode,
                            "output": combined_output[-4000:],
                        },
                        http_status=500,
                    )

                requested_at = time.time()
                log_system_event(
                    "OK", "node_tools", "Telemetry request transmitted",
                    f"Target: {node_name} ({node_id}); Job: {context.job_id}; "
                    "waiting for listener response",
                )
                return ActionResult.success(
                    "Telemetry request sent. Waiting for the node response.",
                    state="waiting_response",
                    data={
                        "output": combined_output[-4000:],
                        "returncode": returncode,
                        "request_sent": True,
                        "telemetry_saved": False,
                        "requested_at": requested_at,
                        "response_timeout_seconds": 50,
                    },
                    http_status=202,
                )

            # Position and traceroute keep their existing synchronous CLI
            # behavior until they are migrated to the same response tracker.
            with radio_lock:
                print(f"[NODE TOOLS CMD] {cmd}", flush=True)
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=context.action.timeout_seconds,
                )

            elapsed = time.time() - start_time
            combined_output = (result.stdout or "").strip()
            print(
                f"[NODE TOOLS] Command finished in {elapsed:.1f}s; job={context.job_id}",
                flush=True,
            )
            print(f"[NODE TOOLS] Return code: {result.returncode}", flush=True)
            print(f"[NODE TOOLS] Output: {combined_output[:2000]}", flush=True)

            if result.returncode != 0:
                error_text = combined_output or f"{action_title} failed"
                error_code, user_message = _friendly_command_error(
                    action_title, error_text, configured_port, resolved_port,
                )
                log_system_event(
                    "ERROR", "node_tools", action_failed_event,
                    f"Target: {node_name} ({node_id}); Job: {context.job_id}; "
                    f"{error_text[:500]}",
                )
                return ActionResult.failure(
                    user_message,
                    error_code=error_code,
                    data={
                        "technical_error": error_text[-2000:],
                        "returncode": result.returncode,
                        "output": combined_output[-4000:],
                    },
                    http_status=500,
                )

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
                        f"Target: {node_name} ({node_id}); Job: {context.job_id}; "
                        f"{position['latitude']}, {position['longitude']}",
                    )

            log_system_event(
                "OK", "node_tools", action_completed_event,
                f"Target: {node_name} ({node_id}); Job: {context.job_id}",
            )
            data = {"output": combined_output[-4000:], "returncode": result.returncode}
            if action == "request_position":
                data["position"] = position
                data["position_saved"] = position is not None
            return ActionResult.success(f"{action_title} completed", data=data)

        except subprocess.TimeoutExpired as error:
            log_system_event(
                "WARNING", "node_tools", f"{action_title} timed out",
                f"Target: {node_name} ({node_id}); Job: {context.job_id}; "
                f"Python timeout after {error.timeout}s",
            )
            return ActionResult.failure(
                f"{action_title} timed out",
                error_code="radio_timeout",
                state="timeout",
                data={"timeout_seconds": error.timeout},
                http_status=504,
            )

        except Exception as error:
            error_code, user_message = _friendly_command_error(
                action_title, str(error), configured_port, resolved_port,
            )
            log_system_event(
                "ERROR", "node_tools", action_failed_event,
                f"Target: {node_name} ({node_id}); Job: {context.job_id}; {error}",
            )
            return ActionResult.failure(
                user_message,
                error_code=error_code,
                data={"technical_error": str(error)},
                http_status=500,
            )

        finally:
            # Do not race channel discovery or the listener against a CLI
            # process that has only just closed its serial descriptor.
            wait_serial_release(device=resolved_port, timeout=6)
            time.sleep(0.4)
            if is_radio_available():
                pause_listen.clear()
            print(
                f"[NODE TOOLS] Listener resume requested; job={context.job_id}",
                flush=True,
            )

    for action_id, title in (
        ("request_position", "Request Position"),
        ("request_telemetry", "Request Telemetry"),
        ("traceroute", "Run Traceroute"),
    ):
        registry.register(ActionDefinition(
            action_id=action_id,
            title=title,
            category="node_tools",
            handler=execute_node_tool,
            requires_radio=True,
            timeout_seconds=70,
        ))

    runner = ActionRunner(registry)

    @app.route("/api/node_tools", methods=["POST"])
    @handle_errors
    def api_node_tools():
        data = request.get_json(force=True) or {}
        action = str(data.get("action", "")).strip()
        node_id = str(data.get("node_id", "")).strip()

        definition = registry.resolve(action)
        if definition is None:
            result = ActionResult.failure(
                "Unsupported node action",
                error_code="unsupported_action",
                http_status=400,
            )
            return jsonify({
                "ok": result.ok,
                "status": result.state,
                "state": result.state,
                "error": result.error,
                "error_code": result.error_code,
            }), result.http_status

        if not node_id or not is_valid_node_id(node_id):
            return jsonify({
                "ok": False,
                "status": "error",
                "state": "error",
                "error": "Invalid node_id",
                "error_code": "invalid_node_id",
            }), 400

        if definition.requires_radio and not is_radio_available():
            return jsonify({
                "ok": False,
                "status": "unavailable",
                "state": "unavailable",
                "action": definition.action_id,
                "action_title": definition.title,
                "node_id": node_id,
                "error": "The radio is released for external configuration",
                "error_code": "radio_released",
            }), 409

        if not node_tools_lock.acquire(blocking=False):
            return jsonify({
                "ok": False,
                "status": "busy",
                "state": "busy",
                "action": definition.action_id,
                "action_title": definition.title,
                "node_id": node_id,
                "error": "Another Node Tools command is already running",
                "error_code": "action_busy",
            }), 409

        try:
            with state_lock:
                node = dict(nodes.get(node_id, {}))
            node_name = node.get("name") or node.get("clean_name") or node_id

            _, result = runner.run(
                action,
                node_id=node_id,
                node_name=node_name,
                request_data=data,
            )

            payload = dict(result.data)
            payload.update({
                "ok": result.ok,
                "status": result.state,
                "state": result.state,
                "message": result.message,
            })
            if not result.ok:
                payload["error"] = result.error or result.message
                payload["error_code"] = result.error_code

            return jsonify(payload), result.http_status

        finally:
            if node_tools_lock.locked():
                node_tools_lock.release()
