"""REST routes for the new pluggable camera-driver framework
(devices/device_driver.py, camera/camera_driver.py, camera/csi_driver.py,
camera/usb_driver.py, camera/camera_manager.py).

Deliberately separate from api/api_camera.py, which still owns
/video_feed and the rest of the live camera.py-backed routes - see the
project's usb-camera-plan notes for why the cutover to camera_manager for
those routes is a distinct, later step, not part of this module.

The CameraManager instance is built lazily, only on an explicit rescan
request (POST /api/devices/cameras/rescan), never automatically at server
startup or on a plain GET. build_camera_manager() calls
CsiCameraDriver.detect(), which calls camera.py's own init_camera()
(Picamera2()) internally - building it automatically at startup, while
camera.py's own unconditional init_camera() call also still runs there
(see server.py's CUTOVER TODO comment), would open a second Picamera2()
while the first is still live. Confirmed live on camtest that even one
extra detect() call while the camera is already claimed produces
"Pipeline handler in use by another process". Requiring an explicit
rescan click keeps that risk to "the user asked for it", matching the
already-agreed "manual rescan only, no hotplug watching" decision for
this whole feature.
"""

from __future__ import annotations

from flask import request, jsonify

from camera.camera_manager import build_camera_manager


def register_camera_manager_routes(app, device_manager, handle_errors):
    # Not built until the first rescan - see the module docstring.
    state = {"manager": None}

    def _summary():
        manager = state["manager"]
        if manager is None:
            return {"ok": True, "scanned": False, "active_id": None, "cameras": []}
        return {
            "ok": True,
            "scanned": True,
            "active_id": manager.active_id,
            "cameras": manager.list_drivers(),
        }

    @app.route("/api/devices/cameras")
    def api_devices_cameras():
        """Whatever was found by the last rescan - never triggers device
        I/O itself, safe to call as often as the UI wants."""
        return jsonify(_summary())

    @app.route("/api/devices/cameras/rescan", methods=["POST"])
    @handle_errors
    def api_devices_cameras_rescan():
        """The only place build_camera_manager() gets called - see the
        module docstring for why that's deliberate."""
        previous = state["manager"]
        if previous is not None:
            # Without this, the previous manager's active driver (its
            # background reader thread + open /dev/videoN, for
            # usb_driver.py) is simply abandoned when state["manager"] is
            # replaced below - confirmed live via lsof that the device
            # stayed held open across a rescan. Same stop-before-replace
            # CameraManager.set_active() already does for a same-manager
            # switch, just applied across the manager replacement here too.
            active = previous.active()
            if active is not None:
                active.stop()

        devices_data = device_manager.load_or_create()
        state["manager"] = build_camera_manager(
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

        manager = state["manager"]
        if manager is None:
            devices_data = device_manager.load_or_create()
            manager = build_camera_manager(
                persisted_active_id=devices_data.get("active_camera_id")
            )
            state["manager"] = manager

        ok = manager.set_active(driver_id)
        if not ok:
            return jsonify({
                "ok": False,
                "error": f"Unknown or failed to start camera: {driver_id}",
            }), 404

        devices_data = device_manager.load_or_create()
        devices_data["active_camera_id"] = driver_id
        device_manager.save(devices_data)

        return jsonify(_summary())
