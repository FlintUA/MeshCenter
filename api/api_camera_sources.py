"""REST routes for saved network camera sources (CAM2, Phase 1a).

    GET    /api/camera/sources          list (password never returned, only password_set)
    POST   /api/camera/sources          create
    PUT    /api/camera/sources/<id>     edit (password: absent = keep, text = replace, clear_password = remove)
    DELETE /api/camera/sources/<id>     delete

CSRF and authentication come from the project-wide before_request hooks
(api/api_auth.py). Write routes are additionally rate limited. After a change
the live CameraManager is brought in line (camera/camera_manager.py
replace_network_drivers) so the Camera tab sees the new source without a
restart, and the persisted active camera id is kept consistent when the active
source was removed.

Same dependency-injection shape as the other api/*.py modules (not a
Blueprint). Error bodies never contain the password, a URL or an exception
message.
"""
from __future__ import annotations

import threading
import time
from collections import deque

from flask import jsonify, request

from camera import source_store as store_module
from camera.camera_manager import network_drivers_from_state
from camera.rtsp_driver import drivers_from_store

_ALLOWED_FIELDS = {
    "name", "host", "port", "username", "password", "clear_password",
    "main_path", "sub_path", "preferred_profile", "transport", "connect_timeout_s",
}
WRITE_LIMIT = 30  # writes per window, all sources together
WRITE_WINDOW_S = 60.0


class _RateLimiter:
    def __init__(self, limit: int, window_s: float, clock=time.monotonic):
        self._limit, self._window, self._clock = limit, window_s, clock
        self._hits: deque = deque()
        self._lock = threading.Lock()

    def allow(self) -> bool:
        now = self._clock()
        with self._lock:
            while self._hits and now - self._hits[0] > self._window:
                self._hits.popleft()
            if len(self._hits) >= self._limit:
                return False
            self._hits.append(now)
            return True


def register_camera_source_routes(app, source_store, camera_manager_state, device_manager, handle_errors, clock=time.monotonic):
    limiter = _RateLimiter(WRITE_LIMIT, WRITE_WINDOW_S, clock)

    def _error(status, code, message, field=""):
        body = {"ok": False, "error": message, "error_code": code}
        if field:
            body["field"] = field
        return jsonify(body), status

    def _body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else None

    def _sync_manager():
        manager = camera_manager_state.get("manager")
        if manager is None:
            return
        before = manager.active_id
        manager.replace_network_drivers(drivers_from_store(source_store))
        if manager.active_id != before:
            devices_data = device_manager.load_or_create()
            devices_data["active_camera_id"] = manager.active_id
            device_manager.save(devices_data)

    def _fields_and_password(data):
        unknown = sorted(set(data) - _ALLOWED_FIELDS)
        if unknown:
            raise store_module.SourceValidationError("unknown_field", "Unknown field: " + unknown[0], unknown[0])
        fields = {k: v for k, v in data.items() if k not in ("password", "clear_password")}
        if data.get("clear_password") is True:
            password = None
        elif "password" in data:
            password = data["password"] if data["password"] != "" else store_module.UNSET
        else:
            password = store_module.UNSET
        return fields, password

    def _guard(action):
        try:
            return action()
        except store_module.SourceValidationError as error:
            status = 409 if error.code in ("duplicate_name", "too_many_sources") else 400
            return _error(status, error.code, error.message, error.field)
        except store_module.SourceNotFound:
            return _error(404, "source_not_found", "Unknown camera source.")
        except store_module.SourceStorageError:
            return _error(500, "storage_write_failed", "Could not save - storage write failed")

    @app.route("/api/camera/sources", methods=["GET"])
    def api_camera_sources_list():
        return jsonify({
            "ok": True,
            "sources": source_store.list_sources(),
            "max_sources": store_module.MAX_SOURCES,
            "profiles": list(store_module.PROFILES),
            "transports": list(store_module.TRANSPORTS),
        })

    @app.route("/api/camera/sources", methods=["POST"])
    @handle_errors
    def api_camera_sources_create():
        if not limiter.allow():
            return _error(429, "rate_limited", "Too many changes, try again in a minute.")
        data = _body()
        if data is None:
            return _error(400, "invalid_body", "Expected a JSON object.")

        def action():
            fields, password = _fields_and_password(data)
            source = source_store.create(fields, None if password is store_module.UNSET else password)
            _sync_manager()
            return jsonify({"ok": True, "source": source}), 201

        return _guard(action)

    @app.route("/api/camera/sources/<source_id>", methods=["PUT"])
    @handle_errors
    def api_camera_sources_update(source_id):
        if not limiter.allow():
            return _error(429, "rate_limited", "Too many changes, try again in a minute.")
        data = _body()
        if data is None:
            return _error(400, "invalid_body", "Expected a JSON object.")

        def action():
            fields, password = _fields_and_password(data)
            source = source_store.update(source_id, fields, password)
            _sync_manager()
            return jsonify({"ok": True, "source": source})

        return _guard(action)

    @app.route("/api/camera/sources/<source_id>", methods=["DELETE"])
    @handle_errors
    def api_camera_sources_delete(source_id):
        if not limiter.allow():
            return _error(429, "rate_limited", "Too many changes, try again in a minute.")

        def action():
            source_store.delete(source_id)
            _sync_manager()
            return jsonify({"ok": True})

        return _guard(action)

    return limiter
