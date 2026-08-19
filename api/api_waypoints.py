"""Waypoint REST API - list/detail/hide/delete/bulk-delete and sending a new
waypoint over the radio.

Extracted 1:1 from server.py's waypoints block - no logic changed, only
decoupled via the same register_<area>_routes(app, ...) dependency-injection
pattern used throughout api/*.py (see CLAUDE.md's Architecture section) -
not a Blueprint.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

from flask import jsonify, request


def register_waypoint_routes(
    app,
    waypoint_store,
    get_node_name,
    handle_errors,
    is_radio_available,
    prepare_radio_command,
    radio_lock,
    pause_listen,
    add_message,
    log_system_event,
    channel_chat_id,
    MESHTASTIC_PORT,
    MESHTASTIC_CMD,
    PROJECT_DIR,
    LOCAL_NODE_ID,
    LOCAL_NODE_NAME,
    CHANNEL_CHAT_NAME,
):
    @app.route("/api/waypoints", methods=["GET"])
    @handle_errors
    def api_waypoints():
        include_expired = request.args.get("include_expired", "0").lower() in ("1", "true", "yes")
        include_hidden = request.args.get("include_hidden", "0").lower() in ("1", "true", "yes")
        include_raw = request.args.get("include_raw", "0").lower() in ("1", "true", "yes")
        waypoints = waypoint_store.list(
            include_expired=include_expired,
            include_hidden=include_hidden,
        )
        for waypoint in waypoints:
            sender_id = waypoint.get("sender_id") or ""
            waypoint["sender_name"] = get_node_name(sender_id) if sender_id else "Unknown"
            if not include_raw:
                waypoint.pop("raw_packet", None)
        return jsonify({
            "ok": True,
            "waypoints": waypoints,
            "total": len(waypoints),
        })

    @app.route("/api/waypoints/<int:waypoint_id>", methods=["GET"])
    @handle_errors
    def api_waypoint_detail(waypoint_id):
        waypoint = waypoint_store.get(waypoint_id)
        if not waypoint:
            return jsonify({"ok": False, "error": "Waypoint not found", "error_code": "waypoint_not_found"}), 404

        sender_id = waypoint.get("sender_id") or ""
        waypoint["sender_name"] = get_node_name(sender_id) if sender_id else "Unknown"
        include_raw = request.args.get("include_raw", "0").lower() in ("1", "true", "yes")
        if not include_raw:
            waypoint.pop("raw_packet", None)
        return jsonify({"ok": True, "waypoint": waypoint})

    @app.route("/api/waypoints/<int:waypoint_id>/hidden", methods=["POST"])
    @handle_errors
    def api_waypoint_hidden(waypoint_id):
        data = request.get_json(silent=True) or {}
        hidden = bool(data.get("hidden", True))
        waypoint = waypoint_store.set_hidden(waypoint_id, hidden)
        if not waypoint:
            return jsonify({"ok": False, "error": "Waypoint not found", "error_code": "waypoint_not_found"}), 404
        sender_id = waypoint.get("sender_id") or ""
        waypoint["sender_name"] = get_node_name(sender_id) if sender_id else "Unknown"
        waypoint.pop("raw_packet", None)
        return jsonify({"ok": True, "waypoint": waypoint})

    @app.route("/api/waypoints/<int:waypoint_id>", methods=["DELETE"])
    @handle_errors
    def api_waypoint_delete(waypoint_id):
        deleted = waypoint_store.delete(waypoint_id)
        if not deleted:
            return jsonify({"ok": False, "error": "Waypoint not found", "error_code": "waypoint_not_found"}), 404
        return jsonify({"ok": True, "deleted": 1, "waypoint_id": waypoint_id})

    @app.route("/api/waypoints/delete", methods=["POST"])
    @handle_errors
    def api_waypoints_delete_many():
        data = request.get_json(silent=True) or {}
        raw_ids = data.get("waypoint_ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "waypoint_ids must be a list", "error_code": "waypoint_ids_not_a_list"}), 400
        try:
            waypoint_ids = [int(value) for value in raw_ids]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid waypoint ID", "error_code": "invalid_waypoint_id"}), 400
        deleted = waypoint_store.delete_many(waypoint_ids)
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/api/waypoints", methods=["DELETE"])
    @handle_errors
    def api_waypoints_delete_all():
        deleted = waypoint_store.delete_all()
        return jsonify({"ok": True, "deleted": deleted})

    @app.route("/api/waypoints/send", methods=["POST"])
    @handle_errors
    def api_waypoint_send():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name") or "").strip()
        description = str(data.get("description") or "").strip()
        latitude = data.get("latitude")
        longitude = data.get("longitude")
        channel_index = data.get("channel_index", 0)
        icon = data.get("icon", 128205)
        expire_at = data.get("expire_at")
        post_notification = bool(data.get("post_notification", True))

        if not name or len(name) > 30:
            return jsonify({"ok": False, "error": "Name is required and must be at most 30 characters", "error_code": "waypoint_invalid_name"}), 400
        if len(description) > 100:
            return jsonify({"ok": False, "error": "Description must be at most 100 characters", "error_code": "waypoint_invalid_description"}), 400
        try:
            latitude = float(latitude)
            longitude = float(longitude)
            channel_index = int(channel_index)
            icon = int(icon)
            expire_at = int(expire_at)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Invalid waypoint data", "error_code": "waypoint_invalid_data"}), 400
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            return jsonify({"ok": False, "error": "Coordinates are outside the valid range", "error_code": "waypoint_invalid_coordinates"}), 400
        if not (0 <= channel_index <= 7):
            return jsonify({"ok": False, "error": "Channel index must be between 0 and 7", "error_code": "waypoint_invalid_channel_index"}), 400
        if expire_at <= int(time.time()) + 30:
            return jsonify({"ok": False, "error": "Expiration must be in the future", "error_code": "waypoint_expiration_in_past"}), 400
        if not is_radio_available():
            return jsonify({"ok": False, "error": "Meshtastic radio is currently unavailable", "error_code": "radio_released"}), 503
        if not prepare_radio_command(MESHTASTIC_PORT, timeout=10):
            return jsonify({"ok": False, "error": "Meshtastic serial port is busy", "error_code": "radio_busy"}), 503

        cli_path = str(MESHTASTIC_CMD or "")
        python_path = os.path.join(os.path.dirname(cli_path), "python3")
        if not os.path.exists(python_path):
            python_path = sys.executable
        sender_script = os.path.join(PROJECT_DIR, "storage", "waypoint_sender.py")

        expires_text = datetime.fromtimestamp(expire_at).strftime("%d.%m.%Y %H:%M")
        notification_text = f"📍 Waypoint: {name}"
        if description:
            notification_text += f"\n{description}"
        notification_text += f"\n{latitude:.6f}, {longitude:.6f}\nExpires: {expires_text}"

        payload = {
            "port": MESHTASTIC_PORT,
            "name": name,
            "description": description,
            "latitude": latitude,
            "longitude": longitude,
            "channel_index": channel_index,
            "icon": icon,
            "expire_at": expire_at,
            "post_notification": post_notification,
            "notification_text": notification_text,
        }

        try:
            with radio_lock:
                result = subprocess.run(
                    [python_path, sender_script],
                    input=json.dumps(payload, ensure_ascii=False),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=35,
                )
            output = (result.stdout or "").strip()
            if result.returncode != 0:
                print(f"[WAYPOINT SEND] Failed: {output}", flush=True)
                return jsonify({"ok": False, "error": output[-1000:] or "Waypoint send failed", "error_code": "waypoint_send_failed"}), 500

            try:
                sender_result = json.loads(output.splitlines()[-1])
                waypoint_id = int(sender_result["waypoint_id"])
            except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Waypoint sender returned invalid result: {output[-1000:]}") from error

            saved = waypoint_store.upsert({
                "waypoint_id": waypoint_id,
                "sender_id": LOCAL_NODE_ID,
                "name": name,
                "description": description,
                "latitude": latitude,
                "longitude": longitude,
                "icon": icon,
                "expire_at": expire_at,
                "channel_index": channel_index,
                "received_at": time.time(),
                "raw_packet": json.dumps({"source": "local-send", **sender_result}, ensure_ascii=False),
            })
            saved.pop("_event", None)
            saved.pop("raw_packet", None)
            saved["sender_name"] = LOCAL_NODE_NAME

            if post_notification:
                channel_id = channel_chat_id(channel_index)
                channel_name = CHANNEL_CHAT_NAME if channel_index == 0 else f"Channel {channel_index}"
                add_message(
                    "me",
                    LOCAL_NODE_NAME,
                    notification_text,
                    node_id=LOCAL_NODE_ID,
                    chat_id=channel_id,
                    chat_name=channel_name,
                    packet_id=sender_result.get("notification_packet_id"),
                )

            print(
                f"[WAYPOINT SEND] Sent and saved: {name}; waypoint_id={waypoint_id}; "
                f"lat={latitude}; lon={longitude}; channel={channel_index}",
                flush=True,
            )
            log_system_event(
                title="Waypoint sent",
                level="OK",
                details=f"{name}; id {waypoint_id}; {latitude:.6f}, {longitude:.6f}; channel {channel_index}",
                source="waypoint",
            )
            return jsonify({
                "ok": True,
                "message": "Waypoint sent",
                "waypoint": saved,
                "notification_posted": post_notification,
                "sender_result": sender_result,
            })
        finally:
            if is_radio_available():
                pause_listen.clear()
