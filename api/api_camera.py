from flask import request, jsonify, Response, send_file
import socket
import struct
from pathlib import Path
from datetime import datetime
import base64
import os
import secrets
import threading
import time
import zipfile

from camera import mjpeg_fallback, send_copy
from camera.ip_snapshot import SnapshotError
from camera.camera_manager import build_camera_manager, network_drivers_from_state
from camera.rtsp_driver import StreamNotReady, ViewerLimitReached
from meshsrv import error_reporting, session_guard
from storage.json_store import safe_read_json, safe_write_json


# Gallery bulk actions (G1): at most this many pictures per request, and one zip download is capped (a Pi Zero streams it
# straight from disk, but a phone should not be handed gigabytes by one tap; the gallery tells the user before they try).
GALLERY_BULK_MAX = 500
GALLERY_ZIP_MAX_BYTES = 250 * 1024 * 1024
ZIP_TICKET_TTL_S = 60.0
ZIP_TICKETS_MAX = 8


class _ZipTickets:
    """One-time download links for a zip: the selection is checked (and the size cap enforced) when the ticket is
    issued with a normal CSRF-protected POST; the browser then downloads with a plain GET, which cannot carry a header."""

    def __init__(self, ttl_s=ZIP_TICKET_TTL_S, limit=ZIP_TICKETS_MAX, clock=time.monotonic):
        self._ttl_s, self._limit, self._clock = ttl_s, limit, clock
        self._items = {}
        self._lock = threading.Lock()

    def _sweep(self, now):
        for key in [k for k, (_names, until) in self._items.items() if until < now]:
            del self._items[key]

    def issue(self, names):
        with self._lock:
            now = self._clock()
            self._sweep(now)
            while len(self._items) >= self._limit:
                del self._items[min(self._items, key=lambda k: self._items[k][1])]
            ticket = secrets.token_urlsafe(24)
            self._items[ticket] = (list(names), now + self._ttl_s)
            return ticket

    def take(self, ticket):
        with self._lock:
            self._sweep(self._clock())
            entry = self._items.pop(ticket, None)
            return None if entry is None else entry[0]


_zip_tickets = _ZipTickets()


class _ChunkSink:
    """A write-only stream for zipfile: collects what it writes so the generator can hand it on (zipfile writes the
    data descriptors itself when the target cannot seek)."""

    def __init__(self):
        self.chunks = []
        self.position = 0

    def write(self, data):
        self.chunks.append(bytes(data))
        self.position += len(data)
        return len(data)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def drain(self):
        data, self.chunks = b"".join(self.chunks), []
        return data


def stream_zip(found, chunk=64 * 1024):
    """Yield a zip (stored, JPEGs do not compress) of [(path, name, size)] without a temp file or the whole thing in
    memory. Entries are named by the picture's own file name (a counter keeps duplicates apart)."""
    sink = _ChunkSink()
    used = set()
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as archive:
        for path, name, _size in found:
            base = os.path.basename(name) or "picture.jpg"
            entry, count = base, 1
            while entry in used:
                count += 1
                stem, ext = os.path.splitext(base)
                entry = f"{stem}_{count}{ext}"
            used.add(entry)
            info = zipfile.ZipInfo.from_file(path, entry)
            info.compress_type = zipfile.ZIP_STORED
            try:
                with open(path, "rb") as source, archive.open(info, "w", force_zip64=True) as target:
                    while True:
                        block = source.read(chunk)
                        if not block:
                            break
                        target.write(block)
                        data = sink.drain()
                        if data:
                            yield data
            except OSError:
                continue  # a picture removed while the zip was being made: leave it out
            data = sink.drain()
            if data:
                yield data
    data = sink.drain()
    if data:
        yield data


def register_camera_routes(app, camera, camera_manager_state, device_manager, handle_errors, ip_snapshot=None):
    power_lock = threading.RLock()

    def _active_driver():
        manager = camera_manager_state.get("manager")
        return manager.active() if manager else None

    def _ensure_manager():
        """Like _active_driver() but builds camera_manager_state["manager"]
        if it doesn't exist yet - needed because server.py deliberately
        skips building it at startup when the camera is persisted off (see
        that file's __main__ block), so turning the camera back on has to
        be able to build it lazily, the same way api_camera_manager.py's
        /api/camera/active already does for its own lazy-build case."""
        manager = camera_manager_state.get("manager")
        if manager is None:
            devices_data = device_manager.load_or_create()
            manager = build_camera_manager(
                persisted_active_id=devices_data.get("active_camera_id"),
                network_drivers=network_drivers_from_state(camera_manager_state),
            )
            camera_manager_state["manager"] = manager
        return manager

    project_dir = Path(__file__).resolve().parents[1]
    data_dir = Path(getattr(camera, "DATA_DIR", project_dir / "data"))
    power_state_file = data_dir / "camera_power.json"

    power_state = {
        "enabled": True,
        "status": "ready",
        "error": None
    }

    def save_power_state():
        # F4.1: folded onto the shared storage.json_store helper (unique
        # temp name + fsync + directory fsync) instead of this route's own
        # fixed-.tmp-name, no-fsync ad-hoc write.
        return safe_write_json(str(power_state_file), {"enabled": bool(power_state["enabled"])})

    def load_power_state():
        saved = safe_read_json(str(power_state_file), default={})
        if not saved:
            return
        power_state["enabled"] = bool(saved.get("enabled", True))

    def _power_error_text(error):
        """What /api/camera/power may show: a generic text + error id, never
        the exception itself (that goes to the logs under the same id)."""
        error_id = error_reporting.report_unexpected_error(error, "camera_power")
        return f"{error_reporting.GENERIC_MESSAGE} (ID: {error_id})"

    def close_camera_device():
        driver = _active_driver()
        if driver is not None:
            driver.stop()

    def start_camera_device():
        manager = _ensure_manager()
        driver = manager.active()
        if driver is None:
            raise RuntimeError("No active camera")
        if not driver.start():
            raise RuntimeError("Camera failed to start")

    def public_power_state():
        manager = camera_manager_state.get("manager")
        status = manager.get_status() if manager else {}
        return {
            "ok": True,
            "enabled": bool(power_state["enabled"]),
            "status": power_state["status"],
            "error": power_state["error"],
            "available": bool(status.get("ok")),
            "started": bool(status.get("started")),
            "mode": None,
        }

    load_power_state()

    if not power_state["enabled"]:
        try:
            close_camera_device()
            power_state["status"] = "off"
        except Exception as error:
            power_state["status"] = "error"
            power_state["error"] = _power_error_text(error)
            print(
                f"[CAMERA POWER] Startup shutdown failed: {error}",
                flush=True
            )
    @app.route('/video_feed')
    def video_feed():
        """MJPEG video stream - dispatches through whichever driver
        camera_manager_state currently has active (CSI or USB), not just
        camera.py's CSI-only path."""
        if not power_state["enabled"]:
            return "Camera is turned off", 409

        manager = camera_manager_state.get("manager")
        if manager is None or manager.active() is None:
            print("[CAMERA] ❌ Camera not available", flush=True)
            return "Camera not available", 503

        active = manager.active()
        if getattr(active, "source_type", None) == "rtsp" and mjpeg_fallback.fallback_disabled_reason():
            # CAM2: the MJPEG path for a network camera is a transcode; refused on low-power hosts
            return jsonify({"ok": False, "error_code": "mjpeg_fallback_disabled",
                            "error": "MJPEG fallback is disabled on this low-power host"}), 503

        return Response(
            session_guard.guard_stream(manager.mjpeg_multipart_stream(), _session_check(), label="MJPEG stream"),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )

    def _session_check():
        """AUTH2: the check a long-lived response asks every few seconds (None when nothing guards sessions)."""
        factory = app.extensions.get("mc_session_guard")
        return factory() if factory else None

    def _active_ip_driver():
        driver = _active_driver()
        return driver if getattr(driver, "source_type", None) == "rtsp" else None

    CLIENT_SEND_TIMEOUT_S = 30

    def _harden_stream_socket():
        """A viewer whose network path dies (Wi-Fi drop, a phone that went to sleep) leaves the server
        blocked in send() with data queued, holding a viewer slot, a server thread and the camera
        upstream until the kernel gives up (about 15 minutes by default). Found in the .104 live test
        (Send-Q 224 KB, rto 120 s). Give the connection a send timeout and TCP_USER_TIMEOUT so a dead
        peer is dropped after CLIENT_SEND_TIMEOUT_S and the slot is released."""
        sock = request.environ.get("gunicorn.socket")
        if sock is None:
            return
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDTIMEO, struct.pack("ll", CLIENT_SEND_TIMEOUT_S, 0))
        except (OSError, struct.error, AttributeError):
            pass
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, CLIENT_SEND_TIMEOUT_S * 1000)
        except (OSError, AttributeError):
            pass

    def _network_camera_guard():
        """The routes below drive the CSI sensor module directly (camera/camera.py globals: settings,
        mode switching, stop, screenshot). With a network camera active they must not touch it: it
        would start Picamera2 and its CPU/RAM cost on a Pi that is only showing an IP stream (found
        in the live test: opening the Camera tab switched the CSI sensor on). None = go ahead."""
        if _active_ip_driver() is not None:
            return jsonify({"ok": False, "error": "Not available for a network camera",
                            "error_code": "not_available_for_network_camera"}), 409
        return None

    @app.route("/api/camera/stream/fmp4")
    def api_camera_stream_fmp4():
        """H.264 remux (no transcode) of the active network camera as fragmented MP4, for
        MediaSource / ManagedMediaSource. One upstream ffmpeg per source; at most
        MAX_VIEWERS_PER_SOURCE / MAX_VIEWERS_TOTAL viewers."""
        if not power_state["enabled"]:
            return jsonify({"ok": False, "error": "Camera is turned off", "error_code": "camera_off"}), 409
        driver = _active_ip_driver()
        if driver is None:
            return jsonify({"ok": False, "error": "The active camera is not a network camera", "error_code": "not_an_ip_source"}), 404
        try:
            stream = driver.open_fmp4()
        except ViewerLimitReached:
            return jsonify({"ok": False, "error": "Too many viewers", "error_code": "viewer_limit"}), 429
        except StreamNotReady as error:
            return jsonify({"ok": False, "error": "Stream not ready", "error_code": "stream_not_ready",
                            "state": error.state, "stream_error": error.error_code}), 503
        _harden_stream_socket()
        response = Response(session_guard.guard_stream(stream, _session_check(), label="fMP4 stream"), mimetype="video/mp4", direct_passthrough=True)
        response.headers["X-Codec"] = stream.mse_codec
        response.headers["Access-Control-Expose-Headers"] = "X-Codec"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Accel-Buffering"] = "no"
        return response

    @app.route("/api/camera/stream/profile", methods=["POST"])
    @handle_errors
    def api_camera_stream_profile():
        driver = _active_ip_driver()
        if driver is None:
            return jsonify({"ok": False, "error": "The active camera is not a network camera", "error_code": "not_an_ip_source"}), 404
        data = request.get_json(silent=True)
        profile = data.get("profile") if isinstance(data, dict) else None
        if profile not in ("sub", "main"):
            return jsonify({"ok": False, "error": "profile must be sub or main", "error_code": "invalid_field"}), 400
        driver.set_profile(profile)
        return jsonify({"ok": True, "status": driver.get_status()})

    @app.route("/api/camera/stream/reconnect", methods=["POST"])
    @handle_errors
    def api_camera_stream_reconnect():
        driver = _active_ip_driver()
        if driver is None:
            return jsonify({"ok": False, "error": "The active camera is not a network camera", "error_code": "not_an_ip_source"}), 404
        driver.reconnect()
        return jsonify({"ok": True, "status": driver.get_status()})

    @app.route("/api/camera/power", methods=["GET"])
    def api_camera_power_get():
        return jsonify(public_power_state())

    @app.route("/api/camera/power", methods=["POST"])
    @handle_errors
    def api_camera_power_set():
        data = request.get_json(force=True) or {}
        enabled = bool(data.get("enabled", False))

        with power_lock:
            if enabled == power_state["enabled"]:
                return jsonify(public_power_state())

            power_state["error"] = None
            power_state["status"] = (
                "starting" if enabled else "stopping"
            )

            try:
                if enabled:
                    start_camera_device()
                    power_state["enabled"] = True
                    power_state["status"] = "ready"
                    print(
                        "[CAMERA POWER] Camera enabled",
                        flush=True
                    )
                else:
                    close_camera_device()
                    power_state["enabled"] = False
                    power_state["status"] = "off"
                    print(
                        "[CAMERA POWER] Camera disabled",
                        flush=True
                    )

                save_power_state()
                return jsonify(public_power_state())

            except Exception as error:
                power_state["status"] = "error"
                power_state["error"] = _power_error_text(error)

                print(
                    f"[CAMERA POWER] Error: {error}",
                    flush=True
                )

                return jsonify(public_power_state()), 500

    @app.route("/api/camera/status")
    def api_camera_status():
        """Статус камеры - whichever driver is currently active. Not
        consumed by the frontend today (checked before the cutover), so
        the field set here doesn't need to exactly match camera.py's old
        get_camera_status() shape."""
        manager = camera_manager_state.get("manager")
        return jsonify(manager.get_status() if manager else {"ok": False})

    @app.route("/api/camera/settings", methods=["GET"])
    def api_camera_settings():
        """Получить текущие настройки видео"""
        return jsonify(camera.get_camera_settings())

    @app.route("/api/camera/settings", methods=["POST"])
    @handle_errors
    def api_camera_update_settings():
        """Обновить настройки камеры"""
        blocked = _network_camera_guard()
        if blocked:
            return blocked
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        data = request.get_json(force=True)
        result, status = camera.update_camera_settings(data)
        return jsonify(result), status

    @app.route("/api/camera/stop", methods=["POST"])
    @handle_errors
    def api_camera_stop():
        """Полностью остановить камеру"""
        blocked = _network_camera_guard()
        if blocked:
            return blocked
        camera.stop_camera()
        return jsonify({
            "ok": True,
            "mode": camera.CAMERA_MODE,
            "started": camera.camera_started
        })

    @app.route("/api/camera/switch_mode", methods=["POST"])
    @handle_errors
    def api_camera_switch_mode():
        """Переключение режима камеры"""
        if _active_ip_driver() is not None:
            # opening the Camera tab always asks for the video mode: for a network camera that is a no-op
            return jsonify({"ok": True, "mode": (request.get_json(silent=True) or {}).get("mode", "video"),
                            "not_applicable": True})
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        data = request.get_json(force=True)
        result, status = camera.api_switch_mode(data)
        return jsonify(result), status

    @app.route("/api/camera/mode/<mode>", methods=["POST"])
    def api_camera_set_mode(mode):
        """Переключить предустановленный режим"""
        blocked = _network_camera_guard()
        if blocked:
            return blocked
        result, status = camera.set_video_mode(mode)
        return jsonify(result), status

    @app.route("/api/camera/screenshot", methods=["POST"])
    @handle_errors
    def api_camera_screenshot():
        """Создать скриншот"""
        ip_driver = _active_ip_driver()
        if ip_driver is not None and ip_snapshot is not None:
            # CAM2 Phase 3a: a network camera's picture (its own snapshot API, or one frame from the running
            # stream): never the CSI sensor, never a second camera session
            if not power_state["enabled"]:
                return jsonify({"ok": False, "error": "Camera is turned off", "error_code": "camera_off"}), 409
            try:
                return jsonify(ip_snapshot(ip_driver))
            except SnapshotError as error:
                return jsonify(error.to_body()), error.status
        blocked = _network_camera_guard()
        if blocked:
            return blocked
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        result = camera.capture_screenshot()

        if result.get("success") or result.get("ok"):
            result["ok"] = True
            return jsonify(result)

        return jsonify({
            "ok": False,
            "error": result.get("error", "Unknown error")
        }), 500

    @app.route("/api/camera/screenshot/<path:filename>")
    def api_camera_screenshot_file(filename):
        """Получить скриншот - serves the same path safe_screenshot_path()
        already validated, instead of re-passing the raw filename to a
        second, independent path-resolution step (send_from_directory's
        own safe_join()). Not a vulnerability fix - both layers already
        agreed on every case tried - just removes the latent fragility of
        two validation points that only work if they happen to stay in
        sync."""
        filepath = camera.safe_screenshot_path(filename)
        if filepath is None or not os.path.isfile(filepath):
            return jsonify({"ok": False, "error": "File not found"}), 404

        if request.args.get("send") is not None:
            # G2: the picture prepared for sending through MCAttach (resized, metadata removed); the browser uploads it
            # with the ordinary POST /api/attachments, one transfer per picture
            if camera.THUMBS_DIRNAME in filepath.split(os.sep) or not filepath.lower().endswith(".jpg"):
                return jsonify({"ok": False, "error": "File not found"}), 404
            try:
                data, (width, height) = send_copy.prepare(filepath, request.args.get("send"))
            except send_copy.SendCopyError as error:
                status = {"too_large": 413, "invalid_size": 400}.get(error.code, 422)
                return jsonify({"ok": False, "error": error.message, "error_code": error.code, **error.extra}), status
            response = Response(data, mimetype="image/jpeg")
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Send-Width"], response.headers["X-Send-Height"] = str(width), str(height)
            return response

        if request.args.get("thumb") and camera.THUMBS_DIRNAME not in filepath.split(os.sep):
            # the ~480 px thumbnail (made on the first request when the background backfill has not reached it yet);
            # the original when it cannot be made
            thumb = camera.ensure_thumbnail(filepath)
            if thumb:
                response = send_file(thumb, mimetype="image/jpeg", max_age=86400, conditional=True)
                response.headers["Cache-Control"] = "private, max-age=86400"
                return response

        return send_file(filepath, mimetype="image/jpeg")

    @app.route("/api/camera/screenshots", methods=["GET"])
    def api_camera_screenshots_list():
        """Список всех скриншотов"""
        result, status = camera.list_screenshots()
        return jsonify(result), status

    # G3: what the schedule action "send camera picture" uses. The same Take picture service as the button (one
    # picture per 2 s per source, gallery limits, sidecar), for a named network camera whether or not it is the active
    # one; the copy for sending comes from the same code as the gallery's Send.
    def _scheduled_source_exists(source_id):
        manager = camera_manager_state.get("manager")
        driver = manager.get_driver(source_id) if manager else None
        return getattr(driver, "source_type", None) == "rtsp"

    def _scheduled_take(source_id):
        from meshsrv.schedule_picture import PictureError

        if not power_state["enabled"]:
            raise PictureError("camera_off", "the camera is turned off")
        manager = camera_manager_state.get("manager")
        driver = manager.get_driver(source_id) if manager else None
        if getattr(driver, "source_type", None) != "rtsp" or ip_snapshot is None:
            raise PictureError("camera_unavailable", "the network camera is not available")
        try:
            result = ip_snapshot(driver)
        except SnapshotError as error:
            raise PictureError(error.code, str(error))
        return {"filename": result["filename"], "display_name": result.get("display_name") or ""}

    def _scheduled_prepare(filename, size_key):
        from meshsrv.schedule_picture import PictureError

        filepath = camera.safe_screenshot_path(filename)
        if filepath is None or not os.path.isfile(filepath) or camera.THUMBS_DIRNAME in filepath.split(os.sep):
            raise PictureError("picture_missing", "the picture was not found")
        try:
            data, _size = send_copy.prepare(filepath, size_key)
        except send_copy.SendCopyError as error:
            raise PictureError(error.code, error.message)
        return data

    app.extensions["mc_camera_picture"] = {
        "source_exists": _scheduled_source_exists,
        "take": _scheduled_take,
        "prepare": _scheduled_prepare,
    }

    @app.route("/api/camera/screenshot/<path:filename>", methods=["DELETE"])
    @handle_errors
    def api_camera_screenshot_delete(filename):
        """Удалить скриншот"""
        result, status = camera.delete_screenshot(filename)
        return jsonify(result), status

    @app.route("/api/camera/screenshots/delete", methods=["POST"])
    @handle_errors
    def api_camera_screenshots_delete_selected():
        """Delete the selected pictures: {"filenames": [relative names]}, at most GALLERY_BULK_MAX at once."""
        data = request.get_json(silent=True)
        names = data.get("filenames") if isinstance(data, dict) else None
        if not isinstance(names, list) or not names or len(names) > GALLERY_BULK_MAX:
            return jsonify({"ok": False, "error": f"Send 1 to {GALLERY_BULK_MAX} filenames", "error_code": "invalid_selection"}), 400
        result, status = camera.delete_screenshots(names)
        return jsonify(result), status

    @app.route("/api/camera/screenshots/zip", methods=["POST"])
    @handle_errors
    def api_camera_screenshots_zip_ticket():
        """Step 1 of a bulk download: check the selection and the size cap, hand back a one-time link. The browser
        then downloads the zip natively (GET, streamed, nothing is held in memory on either side)."""
        data = request.get_json(silent=True)
        names = data.get("filenames") if isinstance(data, dict) else None
        if not isinstance(names, list) or not names or len(names) > GALLERY_BULK_MAX:
            return jsonify({"ok": False, "error": f"Send 1 to {GALLERY_BULK_MAX} filenames", "error_code": "invalid_selection"}), 400
        found = camera.resolve_screenshots(names)
        if not found:
            return jsonify({"ok": False, "error": "None of the pictures exist", "error_code": "not_found"}), 404
        total = sum(size for _path, _name, size in found)
        if total > GALLERY_ZIP_MAX_BYTES:
            return jsonify({"ok": False, "error": "The selection is too large for one download", "error_code": "zip_too_large",
                            "total_bytes": total, "max_bytes": GALLERY_ZIP_MAX_BYTES}), 413
        ticket = _zip_tickets.issue([name for _path, name, _size in found])
        return jsonify({"ok": True, "url": f"/api/camera/screenshots/zip/{ticket}", "count": len(found), "total_bytes": total})

    @app.route("/api/camera/screenshots/zip/<ticket>", methods=["GET"])
    def api_camera_screenshots_zip_download(ticket):
        names = _zip_tickets.take(ticket)
        if names is None:
            return jsonify({"ok": False, "error": "This download link expired", "error_code": "ticket_expired"}), 410
        found = camera.resolve_screenshots(names)
        if not found:
            return jsonify({"ok": False, "error": "None of the pictures exist", "error_code": "not_found"}), 404
        stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        response = Response(stream_zip(found), mimetype="application/zip")
        response.headers["Content-Disposition"] = f'attachment; filename="MeshCenter_pictures_{stamp}.zip"'
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/camera/screenshots", methods=["DELETE"])
    @handle_errors
    def api_camera_screenshots_delete_all():
        """Удалить все скриншоты"""
        result, status = camera.delete_all_screenshots()
        return jsonify(result), status

    @app.route("/api/photo/settings", methods=["GET"])
    def api_photo_settings():
        """Получить настройки фото"""
        return jsonify(camera.get_photo_settings())

    @app.route("/api/photo/settings", methods=["POST"])
    @handle_errors
    def api_photo_update_settings():
        """Обновить настройки фото"""
        data = request.get_json(force=True)
        result, status = camera.update_photo_settings(data)
        return jsonify(result), status

    @app.route("/api/photo/capture", methods=["POST"])
    @handle_errors
    def api_photo_capture():
        """Захват фото для превью - dispatches through whichever driver
        is active. Response shape kept compatible with what the frontend
        actually reads (image_data, preview_resolution) - see
        camera.capture_photo_preview()'s richer shape for what's dropped
        (width/height/quality/mode), none of it read by static/chat.js."""
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        manager = camera_manager_state.get("manager")
        driver = manager.active() if manager else None
        if driver is None:
            return jsonify({"ok": False, "error": "No active camera"}), 503

        jpeg_bytes = driver.capture_photo()
        if not jpeg_bytes:
            return jsonify({"ok": False, "error": "Capture failed"}), 500

        # Read the real dimensions out of the captured JPEG itself rather
        # than manager.get_status()['resolution'] - for usb_driver.py,
        # capture_photo() shoots at the camera's actual max resolution and
        # then restores whatever the live stream was running at before
        # returning (see that method's docstring), so by the time this
        # line runs get_status() already reflects the *restored* stream,
        # not the photo that was just taken.
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(jpeg_bytes)) as img:
                preview_resolution = f"{img.width}x{img.height}"
        except Exception:
            preview_resolution = manager.get_status().get("resolution")

        return jsonify({
            "ok": True,
            "image_data": base64.b64encode(jpeg_bytes).decode("utf-8"),
            "preview_resolution": preview_resolution,
        })

    @app.route("/api/photo/save", methods=["POST"])
    @handle_errors
    def api_photo_save():
        """Сохранить фото в максимальном качестве - dispatches through
        whichever driver is active, same as /api/photo/capture, but also
        persists the result to disk (see camera.py's generic, driver-
        agnostic screenshot helpers below - none of them reference the old
        CSI-only CAMERA_AVAILABLE flag that camera.save_highres_photo()
        used to gate on, which is what made this route return "Camera not
        available" for the USB driver even after capture_photo() itself
        was migrated)."""
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        manager = camera_manager_state.get("manager")
        driver = manager.active() if manager else None
        if driver is None:
            return jsonify({"ok": False, "error": "No active camera"}), 503

        jpeg_bytes = driver.capture_photo()
        if not jpeg_bytes:
            return jsonify({"ok": False, "error": "Capture failed"}), 500

        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(jpeg_bytes)) as img:
                width, height = img.width, img.height
        except Exception:
            width, height = None, None

        dt = datetime.now()
        day_dir = camera.get_screenshot_day_dir(dt)
        filename = camera.make_screenshot_filename(dt, prefix="MC_PHOTO")
        filepath = os.path.join(day_dir, filename)

        with open(filepath, "wb") as f:
            f.write(jpeg_bytes)

        rel_path = os.path.relpath(filepath, camera.SCREENSHOTS_DIR).replace("\\", "/")
        camera.cleanup_old_screenshots(max_mb=500, keep_days=30)

        return jsonify({
            "ok": True,
            "success": True,
            "filename": rel_path,
            "display_name": filename,
            "filepath": filepath,
            "size": os.path.getsize(filepath),
            "width": width,
            "height": height,
        })

    # Read by server.py's __main__ block, at import time - before it
    # decides whether to call build_camera_manager() (real device I/O,
    # including opening the camera briefly to detect() it) at startup.
    # See that file's own comment for why "camera is persisted off" means
    # skipping that entirely rather than detecting-then-immediately-closing.
    return power_state["enabled"]
