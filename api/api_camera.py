from flask import request, jsonify, Response, send_from_directory
from pathlib import Path
import json
import threading



def register_camera_routes(app, camera, handle_errors):
    power_lock = threading.RLock()

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
        camera.stop_camera()

        picam2 = getattr(camera, "picam2", None)

        if picam2 is not None:
            try:
                picam2.close()
                print(
                    "[CAMERA POWER] Picamera2 device closed",
                    flush=True
                )
            except Exception as error:
                print(
                    f"[CAMERA POWER] Close warning: {error}",
                    flush=True
                )

        try:
            camera.picam2 = None
        except Exception:
            pass

        try:
            camera.camera_started = False
        except Exception:
            pass

        try:
            camera.CAMERA_ACTIVE = False
        except Exception:
            pass

    def start_camera_device():
        picam2 = getattr(camera, "picam2", None)

        if picam2 is None:
            initializer = getattr(
                camera,
                "init_camera",
                None
            )

            if not callable(initializer):
                raise RuntimeError(
                    "camera.init_camera() is unavailable"
                )

            if not initializer():
                raise RuntimeError(
                    "Camera initialization failed"
                )

        switcher = getattr(
            camera,
            "switch_camera_mode",
            None
        )

        if not callable(switcher):
            raise RuntimeError(
                "camera.switch_camera_mode() is unavailable"
            )

        video_config = getattr(
            camera,
            "VIDEO_CONFIG",
            {}
        ) or {}

        resolution = video_config.get(
            "resolution"
        )

        fps = video_config.get(
            "fps"
        )

        started = switcher(
            "video",
            resolution=resolution,
            fps=fps
        )

        if not started:
            raise RuntimeError(
                "Camera video mode failed to start"
            )

    def public_power_state():
        return {
            "ok": True,
            "enabled": bool(power_state["enabled"]),
            "status": power_state["status"],
            "error": power_state["error"],
            "available": bool(
                getattr(camera, "CAMERA_AVAILABLE", False)
            ),
            "started": bool(
                getattr(camera, "camera_started", False)
            ),
            "mode": getattr(camera, "CAMERA_MODE", None)
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
        """MJPEG video stream."""
        if not power_state["enabled"]:
            return "Camera is turned off", 409

        if not camera.CAMERA_AVAILABLE:
            print("[CAMERA] ❌ Camera not available", flush=True)
            return "Camera not available", 503

        return Response(
            camera.generate_mjpeg_stream(),
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
        """Статус камеры"""
        return jsonify(camera.get_camera_status())

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
        """Получить скриншот"""
        if not camera.screenshot_exists(filename):
            return jsonify({"ok": False, "error": "File not found"}), 404

        return send_from_directory(
            camera.SCREENSHOTS_DIR,
            filename,
            mimetype="image/jpeg"
        )

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
        """Захват фото для превью"""
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        result, status = camera.capture_photo_preview()
        return jsonify(result), status

    @app.route("/api/photo/save", methods=["POST"])
    @handle_errors
    def api_photo_save():
        """Сохранить фото в максимальном качестве"""
        if not power_state["enabled"]:
            return jsonify({
                "ok": False,
                "error": "Camera is turned off"
            }), 409

        result, status = camera.save_highres_photo()
        return jsonify(result), status
