"""REST routes for the camera-side control of a saved network camera (CAM2 Phase 2).

    POST /api/camera/sources/<id>/control/probe    login + abilities + zoom/focus read + one snapshot (never writes)
    POST /api/camera/sources/<id>/control/trust    pin the certificate the camera presents (user-confirmed SHA-256)
    GET  /api/camera/sources/<id>/control          what is stored: type, scheme, pin, last probe (no network)
    GET  /api/camera/sources/<id>/control/zoom     read the lens position and its valid range (device units)
    POST /api/camera/sources/<id>/control/zoom     {"pos": N}: range-checked, rate limited, read back
    GET  /api/camera/snapshot                      the active camera's picture for browsers without MediaSource

CSRF and authentication come from the project-wide before_request hooks. Answers carry machine codes
(`error_code`, `right` for a missing account permission, `fingerprint` when a certificate has to be
confirmed) and generic texts: never the camera's address, the token, the password or an exception message.
The snapshot answer is `Cache-Control: no-store`. None of these routes touches the RTSP stream: a failing
control API leaves the preview running.
"""
from __future__ import annotations

import hashlib
import time

from flask import Response, jsonify, request

from api.api_camera_sources import _RateLimiter
from camera import reolink_client as rc
from camera.source_store import SourceNotFound

PROBE_LIMIT, PROBE_WINDOW_S = 6, 60.0
ZOOM_READ_LIMIT, ZOOM_READ_WINDOW_S = 60, 60.0

_STATUS = {
    rc.NOT_SUPPORTED: 404,
    rc.LOGIN_FAILED: 401,
    rc.TOO_MANY_SESSIONS: 503,
    rc.PERMISSION_DENIED: 403,
    rc.ZOOM_OUT_OF_RANGE: 400,
    rc.RATE_LIMITED: 429,
    rc.HTTP_NOT_ALLOWED: 409,
    rc.ADDRESS_BLOCKED: 400,
    rc.TLS_UNTRUSTED: 409,
    rc.TLS_PIN_MISMATCH: 409,
}


def register_camera_control_routes(app, source_store, camera_manager_state, control, leases, handle_errors, clock=time.monotonic):
    probe_limiter = _RateLimiter(PROBE_LIMIT, PROBE_WINDOW_S, clock)
    read_limiter = _RateLimiter(ZOOM_READ_LIMIT, ZOOM_READ_WINDOW_S, clock)

    def _fail(error: rc.ReolinkError):
        body = {"ok": False, **error.to_dict()}
        return jsonify(body), _STATUS.get(error.code, 502)

    def _guard(action):
        try:
            return action()
        except rc.ReolinkError as error:
            return _fail(error)
        except SourceNotFound:
            return jsonify({"ok": False, "error": "Unknown camera source.", "error_code": "source_not_found"}), 404

    @app.route("/api/camera/sources/<source_id>/control", methods=["GET"])
    def api_camera_control_get(source_id):
        def action():
            return jsonify({"ok": True, "control": source_store.get_source(source_id)["control"]})
        return _guard(action)

    @app.route("/api/camera/sources/<source_id>/control/probe", methods=["POST"])
    @handle_errors
    def api_camera_control_probe(source_id):
        if not probe_limiter.allow():
            return jsonify({"ok": False, "error": "Too many checks, try again in a minute.", "error_code": "rate_limited"}), 429

        def action():
            return jsonify({"ok": True, "capabilities": control.probe(source_id)})
        return _guard(action)

    @app.route("/api/camera/sources/<source_id>/control/trust", methods=["POST"])
    @handle_errors
    def api_camera_control_trust(source_id):
        if not probe_limiter.allow():
            return jsonify({"ok": False, "error": "Too many attempts, try again in a minute.", "error_code": "rate_limited"}), 429
        data = request.get_json(silent=True)
        fingerprint = data.get("sha256") if isinstance(data, dict) else None

        def action():
            return jsonify({"ok": True, **control.trust(source_id, fingerprint)})
        return _guard(action)

    @app.route("/api/camera/sources/<source_id>/control/zoom", methods=["GET"])
    @handle_errors
    def api_camera_control_zoom_read(source_id):
        if not read_limiter.allow():
            return jsonify({"ok": False, "error": "Too many requests.", "error_code": "rate_limited"}), 429

        def action():
            state = control.zoom_read(source_id)
            return jsonify({"ok": True, "zoom": {
                "position": state["zoom_pos"], "range": list(state["zoom_range"]) if state["zoom_range"] else None,
                "focus_position": state["focus_pos"],
            }})
        return _guard(action)

    @app.route("/api/camera/sources/<source_id>/control/zoom", methods=["POST"])
    @handle_errors
    def api_camera_control_zoom_set(source_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or "pos" not in data:
            return jsonify({"ok": False, "error": "Send {\"pos\": <number>}.", "error_code": "invalid_body"}), 400

        def action():
            return jsonify({"ok": True, **control.set_zoom(source_id, data["pos"])})
        return _guard(action)

    def _viewer_key() -> str:
        raw = f"{request.remote_addr}|{request.headers.get('User-Agent', '')}"
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:24]

    @app.route("/api/camera/snapshot", methods=["GET"])
    def api_camera_snapshot():
        """The shared once-a-second picture for a browser without MediaSource. Authorised like every route
        (login cookie); a viewer holds a lease slot while it keeps asking."""
        manager = camera_manager_state.get("manager")
        driver = manager.active() if manager else None
        if driver is None or getattr(driver, "source_type", None) != "rtsp":
            return jsonify({"ok": False, "error": "The active camera is not a network camera", "error_code": "not_an_ip_source"}), 404
        source_id = driver.id

        def action():
            source = source_store.get_source(source_id)
            ctl = source["control"]
            if ctl["type"] != "reolink" or (ctl["capabilities"] and not ctl["capabilities"].get("snapshot")):
                return jsonify({"ok": False, "error": "This camera has no snapshot function.", "error_code": "snapshot_not_supported"}), 404
            if not leases.touch(source_id, _viewer_key()):
                return jsonify({"ok": False, "error": "Too many viewers", "error_code": "viewer_limit"}), 429
            picture = control.snapshot(source_id)
            response = Response(picture, mimetype="image/jpeg")
            response.headers["Cache-Control"] = "no-store"
            return response
        return _guard(action)

    return {"probe": probe_limiter, "read": read_limiter}
