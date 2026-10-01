"""REST routes for the pluggable camera-driver framework
(devices/device_driver.py, camera/camera_driver.py, camera/csi_driver.py,
camera/usb_driver.py, camera/camera_manager.py).

Shares one CameraManager instance with api/api_camera.py (/video_feed and
the rest of the live-camera routes) via camera_manager_state, a mutable
dict passed in from server.py - see that variable's own comment there for
why a plain shared object doesn't work (route registration happens at
import time, before the manager is actually built in server.py's
__main__ block). Using the same instance as api_camera.py is the whole
point post-cutover: switching the active camera here must actually
change what /video_feed streams from, not just what this module's own
state says.

Rescanning (POST /api/devices/cameras/rescan) rebuilds the manager from
scratch via build_camera_manager() - manual only, no hotplug watching,
per the already-agreed decision for this feature. Not automatic on every
GET, both because it's real device I/O and because rebuilding while
something is actively streaming would need the same stop-before-replace
handling this route already does below.
"""

from __future__ import annotations

from flask import request, jsonify

from camera.camera_manager import build_camera_manager
from camera.usb_driver import ffmpeg_and_v4l2ctl_available


def register_camera_manager_routes(app, device_manager, handle_errors, camera_manager_state):
    def _summary():
        manager = camera_manager_state.get("manager")
        # Cheap (no subprocess, no device I/O) - CAM-1: distinguishes "no
        # USB camera plugged in" from "ffmpeg/v4l-utils aren't installed",
        # so the Devices tab can tell the user what to actually do instead
        # of a generic "no cameras found" (see static/chat.js's
        # renderCameraManagerCards()).
        usb_tooling_missing = not ffmpeg_and_v4l2ctl_available()
        if manager is None:
            return {
                "ok": True, "scanned": False, "active_id": None, "cameras": [],
                "usb_tooling_missing": usb_tooling_missing,
            }
        return {
            "ok": True,
            "scanned": True,
            "active_id": manager.active_id,
            "cameras": manager.list_drivers(),
            "usb_tooling_missing": usb_tooling_missing,
        }

    @app.route("/api/devices/cameras")
    def api_devices_cameras():
        """Whatever camera_manager_state currently holds - built at server
        startup and refreshed by rescan, never triggers device I/O itself,
        safe to call as often as the UI wants."""
        return jsonify(_summary())

    @app.route("/api/devices/cameras/rescan", methods=["POST"])
    @handle_errors
    def api_devices_cameras_rescan():
        """The only place build_camera_manager() gets called after
        startup - see the module docstring for why that's deliberate."""
        previous = camera_manager_state.get("manager")
        if previous is not None:
            # Without this, the previous manager's active driver (its
            # background reader thread + open /dev/videoN, for
            # usb_driver.py) is simply abandoned when the manager is
            # replaced below - confirmed live via lsof that the device
            # stayed held open across a rescan. Same stop-before-replace
            # CameraManager.set_active() already does for a same-manager
            # switch, just applied across the manager replacement here too.
            active = previous.active()
            if active is not None:
                active.stop()

        devices_data = device_manager.load_or_create()
        camera_manager_state["manager"] = build_camera_manager(
            persisted_active_id=devices_data.get("active_camera_id")
        )
        return jsonify(_summary())

    @app.route("/api/camera/active", methods=["POST"])
    @handle_errors
    def api_camera_active():
        data = request.get_json(force=True) or {}
        driver_id = str(data.get("driver_id", "")).strip()
        if not driver_id:
            return jsonify({"ok": False, "error": "driver_id is required"}), 400

        manager = camera_manager_state.get("manager")
        if manager is None:
            devices_data = device_manager.load_or_create()
            manager = build_camera_manager(
                persisted_active_id=devices_data.get("active_camera_id")
            )
            camera_manager_state["manager"] = manager

        ok = manager.set_active(driver_id)
        if not ok:
            return jsonify({
                "ok": False,
                "error": f"Unknown or failed to start camera: {driver_id}",
            }), 404

        devices_data = device_manager.load_or_create()
        devices_data["active_camera_id"] = driver_id
        if not device_manager.save(devices_data):
            # The camera switch above already succeeded live - only the
            # "which camera was active" bookkeeping might not survive a
            # restart. Reported as a failure anyway for consistency with
            # every other storage_write_failed response.
            return jsonify({"ok": False, "error": "Could not save - storage write failed", "error_code": "storage_write_failed"}), 500

        return jsonify(_summary())
