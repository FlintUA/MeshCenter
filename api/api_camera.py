from flask import request, jsonify, Response, send_file
from pathlib import Path
from datetime import datetime
import base64
import json
import os
import threading

from camera.camera_manager import build_camera_manager


def register_camera_routes(app, camera, camera_manager_state, device_manager, handle_errors):
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
                persisted_active_id=devices_data.get("active_camera_id")
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
        data_dir.mkdir(parents=True, exist_ok=True)
        temp_file = power_state_file.with_suffix(".json.tmp")

        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(
                {"enabled": bool(power_state["enabled"])},
                file,
                ensure_ascii=False,
                indent=2
            )
            file.flush()

        temp_file.replace(power_state_file)

    def load_power_state():
        try:
            if not power_state_file.exists():
                return

            with power_state_file.open("r", encoding="utf-8") as file:
                saved = json.load(file)

            power_state["enabled"] = bool(
                saved.get("enabled", True)
            )

        except Exception as error:
            print(
                f"[CAMERA POWER] Could not load state: {error}",
                flush=True
            )

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
            power_state["error"] = str(error)
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

        return Response(
            manager.mjpeg_multipart_stream(),
            mimetype='multipart/x-mixed-replace; boundary=frame'
        )

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
                power_state["error"] = str(error)

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
        result, status = camera.set_video_mode(mode)
        return jsonify(result), status

    @app.route("/api/camera/screenshot", methods=["POST"])
    @handle_errors
    def api_camera_screenshot():
        """Создать скриншот"""
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

        return send_file(filepath, mimetype="image/jpeg")

    @app.route("/api/camera/screenshots", methods=["GET"])
    def api_camera_screenshots_list():
        """Список всех скриншотов"""
        result, status = camera.list_screenshots()
        return jsonify(result), status

    @app.route("/api/camera/screenshot/<path:filename>", methods=["DELETE"])
    @handle_errors
    def api_camera_screenshot_delete(filename):
        """Удалить скриншот"""
        result, status = camera.delete_screenshot(filename)
        return jsonify(result), status

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
